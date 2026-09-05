---
name: quota-burndown
description: Read live Codex and Claude quota capacity and freshness, inspect token accounting, and open the local dashboard. Use for allowance remaining, burn rate, reserves or session/model usage.
---

# Live capacity and token usage

Read shared capacity first:

```
__QB_PY__ -B "__QB_LAUNCHER__" capacity --json
```

Report shared pools, remaining/reserve-adjusted allowance, constraining window, reset, freshness and health. Models sharing a limit ID share allowance. Check all windows, including weekly exhaustion. Unknown/stale windows and unverified account scopes are not confirmed capacity. The orchestrator owns scheduling; never promise tokens, jobs or concurrency from percentages. Use `capacity --watch` for requested streams.

For token accounting, run `usage --days 7`, optionally `--by model_effort,session`, date filters, or CSV/JSON exports. Raw accounting is preserved; dashboard input normalizes Codex cache-inclusive totals. Antigravity inferred tokens remain outside exact totals and its quota is unknown.

Open the existing loopback URL in `~/.quota-burndown/capacity-service.json`, default `http://127.0.0.1:8787/`. If unavailable, `capacity` labels its persisted snapshot disconnected. For an authorized live dashboard request, start `serve` once. `render` produces a dated fallback.

Installation changes settings/tasks only when setup is requested: `install --service --apply`, `install --statusline --apply`, or `install --all --apply`. Identical Claude redraws do not renew freshness. Native Codex read completion and provider reporting delay are separate facts.
