---
name: burndown
description: Show the Claude and Codex subscription quota burndown (percent used versus linear pace per rate-limit window, time left, projected exhaustion) plus per-request token usage across Claude Code, Codex and Antigravity (prompts, requests, model, effort, input / cache / output tokens), and open the page. Use when the user asks about quota, rate limits, usage pace, token usage, how many tokens or prompts a tool or model consumed, how much Claude or Codex budget is left, whether they are over or under pace, or invokes /burndown.
---

# Quota burndown and token usage

The tool lives at the plugin root. Its launcher is `${CLAUDE_PLUGIN_ROOT}/quota-burndown.py`.

1. Refresh and print the summary:

   ```
   py "${CLAUDE_PLUGIN_ROOT}/quota-burndown.py" collect --render --quiet
   py "${CLAUDE_PLUGIN_ROOT}/quota-burndown.py" status
   py "${CLAUDE_PLUGIN_ROOT}/quota-burndown.py" usage --days 7
   ```

   Show the status and usage output verbatim in code blocks. Then add one sentence per provider saying whether the user is over or under pace and what that means for the time left in the window. "Over pace" means the budget is being spent faster than a straight line from 0% at window start to 100% at reset; the projection line says when 100% would land at the current rate.

   `usage` accepts `--since/--until YYYY-MM-DD` (UTC dates), `--by provider,model,effort,model_effort,day,session,thread,tool`, `--raw`, and `--csv PATH` / `--json PATH` exports. Antigravity rows are marked inferred: Google records no token counts locally, so its `~input` figure is an unlabeled context counter and is never added into the exact totals.

2. Open the page when a browser surface is available. Prefer the Browser pane: start the server in the background and navigate to it.

   ```
   py "${CLAUDE_PLUGIN_ROOT}/quota-burndown.py" serve --port 8787
   ```

   Then open `http://127.0.0.1:8787/` in the Browser pane. If port 8787 is already serving, just navigate to it. If no browser surface exists, tell the user the static page path printed by `status` (it is `~/.quota-burndown/burndown.html` and refreshes itself every two minutes when the scheduled task is installed).

3. Do not change settings, hooks, or scheduled tasks from this skill. Installation is an explicit, separate step:

   ```
   py "${CLAUDE_PLUGIN_ROOT}/quota-burndown.py" install --apply
   ```

   Only run it if the user asks to install or wire up the tool.

If `status` reports a stale sample, say so plainly: Claude readings come from the desktop app's own history file and only advance while the app runs; Codex readings come from its rollout files and only advance while Codex is used. The tool never signs in or calls a network endpoint.
