---
name: signals
description: Show recent signal evaluations across all runners and why they were blocked (MTF gate, AI overlay, risk check, session filter, etc.). Answers "why didn't my strategy trade?"
allowed-tools: Bash Read
argument-hint: '[strategy-name-filter]'
---

Show recent signal activity across the fleet. Filter to `$ARGUMENTS` if provided (e.g., `/signals usdjpy`).

1. **Argus FX runners** — tail runner_unified.log for SIGNAL / ENTRY / MTF_BLOCK / AI_SKIP / RECON_DRIFT:
   ```bash
   grep -E "SIGNAL|ENTRY|MTF_BLOCK|AI_SKIP|AI TAKE|AI OVERLAY SKIP|NOTIONAL_CAP" C:/Argus/repo/argus_flow/logs/runner_unified.log | tail -30
   ```

2. **Per-strategy signal CSVs** — show last 10 entries for each active runner:
   ```bash
   for s in argus_flow/logs/{usdjpy,gbpusd,cadjpy}; do
     echo "=== $s ==="
     tail -10 "C:/Argus/repo/$s/signals.csv" 2>/dev/null | head -11
   done
   for s in forge/logs/{jpy_pm_short,nq_overnight,gld_pm_long,wick_gbpusd}; do
     echo "=== $s ==="
     tail -5 "C:/Argus/repo/$s/signals.csv" 2>/dev/null | head -6
   done
   ```

3. **Block-reason histogram** — parse runner_unified.log lookback 24h, count each block/skip reason:
   - `MTF_BLOCK` count
   - `AI OVERLAY SKIP` count
   - `MAX_POSITIONS` count
   - `DRAWDOWN_PAUSE` count
   - `NOTIONAL_CAP` warnings
   - `RECON_DRIFT` count

4. **Summary table** per active strategy:
   | Strategy | Signals fired | Entries taken | Top block reason | Last entry |

5. Honest take: if a strategy has many signals but zero entries, name the top block reason and explain what it means. If no signals at all, note whether it's just rare-by-design or whether the runner is stuck.

Argument handling: if `$ARGUMENTS` is non-empty, filter all output to lines mentioning that strategy/symbol. If empty, show everything.
