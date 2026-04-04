---
name: promote
description: Promote or demote a pair between stages (watcher/paper/real). Usage /promote AUDJPY or /promote AUDJPY demote
allowed-tools: Bash Read
argument-hint: '[symbol] [action]'
disable-model-invocation: true
---

Promote or demote $ARGUMENTS.

Parse arguments:
- First arg = symbol
- Second arg = action (promote, demote, kill, revive) — default: promote

For Argus pairs:
- Use the dashboard API: `curl -X POST http://127.0.0.1:8080/api/stage_action -H "Content-Type: application/json" -d '{"action":"...", "symbol":"...", "reason":"manual via /promote skill"}'`

For Helio family pairs:
- Update the config file's `deployment.stage` field
- Run `python -m helio.promotion_check` to verify gates

IMPORTANT: Always show the current stage and promotion gate status before executing. Confirm with the user.
