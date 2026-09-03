# quota-burndown

A burndown of your Claude and Codex subscription quotas: how much of each rate-limit window you have used versus a straight-line pace, how long is left, and when you would hit 100% at the current rate. Works as a Claude Code plugin, a Claude Code status line, a Codex skill, and a self-refreshing local HTML page. Standard library only, Python 3.10+.

## How it works

| Provider | Source | Trigger |
| --- | --- | --- |
| Claude | The OAuth usage endpoint Claude Code uses for `/usage` (5h session, 7-day all models, 7-day per-model windows). Authorized with the OAuth session in `~/.claude/.credentials.json`; the token is read only to make the call and is never logged or stored. | Scheduled task every 5 min, the plugin's `Stop` hook after each turn (debounced), and every page load in `serve` mode. |
| Claude | The `rate_limits` object Claude Code pipes to the status line command. | Every status line refresh in the CLI. No network call. |
| Codex | The `rate_limits` block Codex writes on every `token_count` event in its rollout files under `~/.codex/archived_sessions` and `~/.codex/sessions`. Scanned incrementally by byte offset. | Scheduled task and page loads. Full history import with `backfill`. |

Samples land in `~/.quota-burndown/samples.jsonl` (one JSON line each). `latest.json` holds the newest reading per window so the status line stays fast. The page is `~/.quota-burndown/burndown.html`.

Pace is linear: 0% at window start, 100% at reset. Window start is `resets_at` minus the window length. "Over pace" means you are spending faster than that line; the projection extends your average rate since window start to show either the time you would hit 100% or your projected use at reset.

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
py quota-burndown.py status              # text summary per window
py quota-burndown.py collect --render    # sample both providers and rewrite the page
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

## Limitations

- The Claude usage endpoint is the one Claude Code itself calls, but it is not publicly documented and may change. The `limits` list is parsed first with a fallback to the older `five_hour` / `seven_day` fields.
- The Claude OAuth session expires after a few hours of Claude Code inactivity. The collector skips the call with a warning until Claude Code refreshes it; Codex sampling is unaffected.
- Which Codex windows appear depends on the plan; only windows Codex reports are shown.
- The Claude Code desktop app has no status line. There, use `/quota-burndown:burndown` to open the page in the Browser pane, or open `burndown.html` directly.
