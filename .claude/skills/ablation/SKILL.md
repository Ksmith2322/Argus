---
name: ablation
description: Run ablation test on a pair to identify which signal components drive edge. Usage /ablation EURUSD
allowed-tools: Bash Read
argument-hint: '[symbol]'
disable-model-invocation: true
---

Run ablation test for $ARGUMENTS to identify signal components.

1. **Generate ablation configs** if not already present:
   ```bash
   python -m argus_flow.ops.ablation_test --source $SYMBOL
   ```

2. **Run each variant through backtest**:
   - baseline (no changes)
   - no_range_pct (range_pct_min=0)
   - no_session (all hours)
   - no_direction (neutral thresholds)
   - session_only (no range_pct, no accel)

3. **Compare results** in a table: variant, trades, PnL, PF, WR

4. **Identify**: which component removal causes the biggest PnL drop = the real signal

5. **Verdict**: "range_pct IS/IS NOT the signal" + "session IS/IS NOT critical"
