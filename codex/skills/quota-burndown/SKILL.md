---
name: quota-burndown
description: Show the Codex and Claude subscription quota burndown (percent used versus linear pace per rate-limit window, time left, projected exhaustion) plus per-request token usage across Codex, Claude Code and Antigravity (prompts, requests, model, effort, input / cache / output tokens), and open the page. Use when the user asks about quota, rate limits, usage pace, token usage, how many tokens or prompts a tool or model consumed, how much Codex or Claude budget is left, or whether they are over or under pace.
---

# Quota burndown and token usage

The tool is a stdlib-only Python CLI at `__QB_LAUNCHER__`. It reads Codex rollout files under `~/.codex` (which already carry `rate_limits` and `token_count` on every turn), Claude Code's transcripts and usage endpoint, and Antigravity's conversation store; keeps a local sample store and usage ledger in `~/.quota-burndown`; and renders one page.

1. Refresh and print the summary:

   ```
   __QB_PY__ "__QB_LAUNCHER__" collect --render --quiet
   __QB_PY__ "__QB_LAUNCHER__" status
   __QB_PY__ "__QB_LAUNCHER__" usage --days 7
   ```

   Show the status and usage output verbatim, then one sentence per provider on whether the user is over or under pace and what that means for the time left in the window. "Over pace" means the budget is being spent faster than a straight line from 0% at window start to 100% at reset. `usage` accepts `--since/--until YYYY-MM-DD` (UTC dates), `--by provider,model,effort,model_effort,day,session,thread,tool`, `--raw`, `--csv PATH` and `--json PATH`. Antigravity token figures are inferred and kept out of the exact totals; say so if asked.

2. If a browser surface is available, start the local page and open it:

   ```
   __QB_PY__ "__QB_LAUNCHER__" serve --port 8787
   ```

   Then open `http://127.0.0.1:8787/`. Otherwise tell the user the static page is at `~/.quota-burndown/burndown.html`.

3. Never modify settings, hooks, or scheduled tasks from this skill. `install --apply` is a separate, explicit step the user must ask for.
