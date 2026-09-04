# quota-burndown

One local page for three coding agents. A burndown of your Claude and Codex subscription quotas: how much of each rate-limit window you have used versus a straight-line pace, how long is left, and when you would hit 100% at the current rate. Underneath it, a per-request token usage ledger for Claude Code, Codex and Antigravity: prompts, requests, model, reasoning effort, input / cache read / cache write / output tokens, with timestamps. Works as a Claude Code plugin, a Claude Code status line, a Codex skill, and a self-refreshing local HTML page. Standard library only, Python 3.10+. Nothing leaves the machine.

## How it works

| Provider | Source | Trigger |
| --- | --- | --- |
| Claude | The desktop app's own usage history, `plan-usage-history.json`, which the app appends to every 5 to 15 minutes while it runs (5h and 7-day percentages). No credential, no network call. The app is an MSIX package, so the file it writes to `%APPDATA%\Claude` really lives under `%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude`; the collector checks both and reads the newer copy. The file has no reset times, so the 5h reset is inferred from the first non-zero reading after a zero plus five hours and the 7-day reset from the last drop to zero plus seven days; it has no per-model figure, so per-model windows are not tracked. | Scheduled task every 5 min, and every page load in `serve` mode. |
| Codex | The `rate_limits` block Codex writes on every `token_count` event in its rollout files under `~/.codex/archived_sessions` and `~/.codex/sessions`. Scanned incrementally by byte offset. Codex keeps separate pools per model family, so windows are keyed by the family of the thread's model (`7d:gpt-5.6`, `5h:spark`, `7d:spark`) and each gets its own card. | Scheduled task and page loads. History import with `backfill [--since-days N] [--rescan]`. |

Samples land in `~/.quota-burndown/samples.jsonl` (one JSON line each). `latest.json` holds the newest reading per window so the status line stays fast. The page is `~/.quota-burndown/burndown.html`.

One process generates everything: the Windows scheduled task `QuotaBurndownCollect` runs `collect --render --quiet` every 5 minutes, which samples both quotas, ingests new usage, and rewrites the page (the page reloads itself every 2 minutes). `install --task --apply` registers it to run on battery as well as mains, to catch up one missed run after the laptop wakes, and to give up after 10 minutes; `where` reports missed runs, so a stale page has a visible cause. It runs only while you are logged in. Nothing inside Claude Code runs on a schedule: the plugin ships only the on-demand `/quota-burndown:burndown` skill, and the status line just displays `latest.json`.

Pace is linear: 0% at window start, 100% at reset. Window start is `resets_at` minus the window length. "Over pace" means you are spending faster than that line; the projection extends your average rate since window start to show either the time you would hit 100% or your projected use at reset.

Each card has one chart: % used over the last 24 hours (5-hour windows) or the last 7 days (weekly windows), extended to the current reset so the dashed pace line and dotted projection fit. The Range control above the cards switches every chart to 24 hours, 3, 7, 14 or 30 days (Auto restores each card's default); the choice is remembered by the browser. Every window is its own segment with a tick at its reset; hover or focus a chart and use the arrow keys to read exact readings, or open the table under it. The page is still one local file; its only script is the hover layer.

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

## No credentials, no network

Everything on the page is read from files the tools already write on this machine. The tool never signs in, never stores a token, and never calls a network endpoint. The cost is that a figure which exists only behind an authenticated endpoint, such as Claude's per-model weekly window, is simply not shown. Claude cards stay fresh while the desktop app is running; Codex cards while Codex is used.

## Limitations

- The Claude usage endpoint is the one Claude Code itself calls, but it is not publicly documented and may change. The `limits` list is parsed first with a fallback to the older `five_hour` / `seven_day` fields.
- Claude limit readings only arrive while the desktop app is running; Codex sampling and the usage ledger are unaffected either way.
- Which Codex windows appear depends on the plan; only windows Codex reports are shown.
- The Claude Code desktop app has no status line. There, use `/quota-burndown:burndown` to open the page in the Browser pane, or open `burndown.html` directly.
