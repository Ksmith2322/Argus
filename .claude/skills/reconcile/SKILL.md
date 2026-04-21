---
name: reconcile
description: Diff per-strategy trades.csv vs canonical_fills.jsonl. Flags any strategy whose local trades don't match the canonical aggregate — catches missing dual-writes and schema drift.
allowed-tools: Bash
---

Reconcile per-strategy trade ledgers with canonical_fills.

1. **Collect per-strategy trade counts + PnL from canonical**:
   ```bash
   cd C:/Argus/repo && C:/Argus/.venv/Scripts/python.exe -c "
   import json, collections
   per = collections.defaultdict(lambda: {'n':0, 'pnl':0.0})
   with open('argus_flow/logs/canonical_fills.jsonl') as f:
       for line in f:
           try: r = json.loads(line)
           except: continue
           s = r.get('strategy','?'); pnl = r.get('pnl_usd') or 0
           per[s]['n'] += 1
           try: per[s]['pnl'] += float(pnl)
           except: pass
   for s, v in sorted(per.items()):
       print(f'  canonical  {s:25s} n={v[\"n\"]:4d}  pnl=\${v[\"pnl\"]:>+12.2f}')"
   ```

2. **Walk each strategy's local trades.csv**, count rows + sum pnl_usd:
   - `forge/logs/jpy_pm_short/trades.csv`
   - `forge/logs/nq_overnight/trades.csv`
   - `forge/logs/gld_pm_long/trades.csv`
   - `forge/logs/wick_gbpusd/trades.csv`
   - `forge/logs/gdx_gld/trades.csv`
   - `forge/logs/tori/paper_trades.csv`
   - `forge/logs/cuebanks/paper_trades.csv`
   - `argus_flow/logs/{usdjpy,gbpusd,cadjpy}/trades.csv`
   - `apollo/logs/trades.csv`, `titan/logs/trades.csv`, `hermes/logs/trades.csv` if present

3. **Compare**: for each strategy, show `local.n vs canonical.n` and `local.pnl vs canonical.pnl`. Flag any >1 trade difference or >$1 PnL difference.

4. **Likely causes when they diverge**:
   - Missing `helio.canonical_fills.write_fill_typed` in the runner's close path
   - Schema-drift: stale CSV header while new rows use extended columns
   - Backfill desync

5. **Output a clean table**:
   | Strategy | Local n | Canonical n | Local PnL | Canonical PnL | Status |

   Status = OK, MISSING_CANONICAL_DUAL_WRITE, SCHEMA_DRIFT, or NEEDS_BACKFILL.

6. If all sources match: "All N strategies reconciled. Canonical is trustworthy."

Context: 2026-04-21 incident had jpy_pm_short showing +$14K in per-strategy CSV but only -$200 in canonical — schema drift + missing dual-write. This skill catches that class of bug.
