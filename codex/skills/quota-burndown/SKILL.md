---
name: quota-burndown
description: Show the Codex and Claude subscription quota burndown (percent used versus linear pace per rate-limit window, time left, projected exhaustion) and open the burndown page. Use when the user asks about quota, rate limits, usage pace, how much Codex or Claude budget is left, or whether they are over or under pace.
---

# Quota burndown

The tool is a stdlib-only Python CLI at `__QB_LAUNCHER__`. It reads Codex rollout files under `~/.codex` (which already carry `rate_limits` on every turn) and Claude Code's usage data, keeps a local sample store in `~/.quota-burndown`, and renders a burndown page.

1. Refresh and print the summary:

   ```
   __QB_PY__ "__QB_LAUNCHER__" collect --render --quiet
   __QB_PY__ "__QB_LAUNCHER__" status
   ```

   Show the status output verbatim, then one sentence per provider on whether the user is over or under pace and what that means for the time left in the window. "Over pace" means the budget is being spent faster than a straight line from 0% at window start to 100% at reset.

2. If a browser surface is available, start the local page and open it:

   ```
   __QB_PY__ "__QB_LAUNCHER__" serve --port 8787
   ```

   Then open `http://127.0.0.1:8787/`. Otherwise tell the user the static page is at `~/.quota-burndown/burndown.html`.

3. Never modify settings, hooks, or scheduled tasks from this skill. `install --apply` is a separate, explicit step the user must ask for.
