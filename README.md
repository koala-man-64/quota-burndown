# quota-burndown

One local page for three coding agents. A burndown of your Claude and Codex subscription quotas: how much of each rate-limit window you have used versus a straight-line pace, how long is left, and when you would hit 100% at the current rate. Underneath it, a per-request token usage ledger for Claude Code, Codex and Antigravity: prompts, requests, model, reasoning effort, input / cache read / cache write / output tokens, with timestamps. Works as a Claude Code plugin, a Claude Code status line, a Codex skill, and a self-refreshing local HTML page. Standard library only, Python 3.10+. Nothing leaves the machine.

## How it works

| Provider | Source | Trigger |
| --- | --- | --- |
| Claude | The OAuth usage endpoint Claude Code uses for `/usage` (5h session, 7-day all models, 7-day per-model windows). Authorized with the OAuth session in `~/.claude/.credentials.json`; the token is read only to make the call and is never logged or stored. | Scheduled task every 5 min, the plugin's `Stop` hook after each turn (debounced), and every page load in `serve` mode. |
| Claude | The `rate_limits` object Claude Code pipes to the status line command. | Every status line refresh in the CLI. No network call. |
| Codex | The `rate_limits` block Codex writes on every `token_count` event in its rollout files under `~/.codex/archived_sessions` and `~/.codex/sessions`. Scanned incrementally by byte offset. | Scheduled task and page loads. Full history import with `backfill`. |

Samples land in `~/.quota-burndown/samples.jsonl` (one JSON line each). `latest.json` holds the newest reading per window so the status line stays fast. The page is `~/.quota-burndown/burndown.html`.

Pace is linear: 0% at window start, 100% at reset. Window start is `resets_at` minus the window length. "Over pace" means you are spending faster than that line; the projection extends your average rate since window start to show either the time you would hit 100% or your projected use at reset.

## Token usage ledger

`collect` also parses each tool's own local files into `~/.quota-burndown/usage.sqlite`, one row per model call (`request`) and one per human turn (`prompt`). Files are re-read only when their size or mtime changes, and rows are keyed by the source's own identifiers, so re-scans are idempotent. The ledger outlives the sources: Claude Code deletes transcripts after `cleanupPeriodDays` (30 by default), the ledger does not.

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

`--since/--until` are UTC calendar dates so they line up with the standalone audit scripts; the page's "today" is the local calendar day, its 7- and 30-day figures are rolling windows. `collect --provider claude` (what the plugin's Stop hook runs) parses only changed Claude transcripts within a 10 s budget; `collect` from the scheduled task covers all three tools with a 60 s budget and defers anything it did not reach to the next run. `serve` also answers `/usage.json`.

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

The plugin adds `/quota-burndown:burndown` and a `Stop` hook that samples the Claude quota after each turn. For a one-off session use `claude --plugin-dir C:\Users\rdpro\Projects\quota-burndown`.

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

Status line format: `Claude 5h 44% p38 ▲6 · 7d 27% p45 ▼18 │ Codex 7d 99% p52 ▲47`. `p` is pace, the arrow is points over (▲, red) or under (▼, green) pace.

## Development

```
py -m pytest
```

## Unattended Claude sampling

The Claude Code CLI's OAuth session in `~/.claude/.credentials.json` expires a few hours after the CLI last ran, and only the CLI refreshes it; the desktop app keeps its own session elsewhere. If you mostly use the desktop app, the collector will log `CLI OAuth session expired` and Claude samples stop. Fix it once with a long-lived session:

```
claude setup-token
```

Authorize in the browser, then save the printed value (nothing else) to `~/.quota-burndown/claude_oauth`, for example in PowerShell:

```
Set-Content -Path "$env:USERPROFILE\.quota-burndown\claude_oauth" -Value "<paste>" -NoNewline
icacls "$env:USERPROFILE\.quota-burndown\claude_oauth" /inheritance:r /grant:r "$env:USERNAME:R"
```

Then `py quota-burndown.py collect` should report fresh Claude samples. Precedence is the `QUOTA_BURNDOWN_CLAUDE_OAUTH` environment variable, then that file, then the CLI credentials. The value is only ever sent as a bearer header to the usage endpoint; it is never logged. If the endpoint later rejects it, the log says so and `claude setup-token` again replaces it.

## Limitations

- The Claude usage endpoint is the one Claude Code itself calls, but it is not publicly documented and may change. The `limits` list is parsed first with a fallback to the older `five_hour` / `seven_day` fields.
- Without a long-lived session (above), Claude samples only flow while the CLI's OAuth session is fresh; Codex sampling is unaffected either way.
- Which Codex windows appear depends on the plan; only windows Codex reports are shown.
- The Claude Code desktop app has no status line. There, use `/quota-burndown:burndown` to open the page in the Browser pane, or open `burndown.html` directly.
