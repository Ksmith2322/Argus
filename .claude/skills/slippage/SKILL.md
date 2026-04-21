---
name: slippage
description: Per-pair and fleet-level slippage report from trades.csv. Surfaces paper-mode artifact where execution_mode=paper produces zero slippage (not real-world execution).
allowed-tools: Bash
argument-hint: '[pair-symbol]'
---

Show slippage distribution across the fleet.

1. Run the report:
   ```bash
   cd C:/Argus/repo && C:/Argus/.venv/Scripts/python.exe -m helio.slippage_report
   ```

2. If `$ARGUMENTS` is provided (e.g., `/slippage GBPUSD`), filter to that pair:
   ```bash
   cd C:/Argus/repo && C:/Argus/.venv/Scripts/python.exe -m helio.slippage_report --pair "$ARGUMENTS"
   ```

3. Interpret output:
   - `slippage_pips mean / median / max` per pair
   - `zero_fraction` — if >0.95, report prominently as PAPER_MODE_ARTIFACT (fills at signal_mid, not broker-simulated)
   - `fleet.weighted_mean_slippage_pips`

4. Key honesty check:
   - If zero_fraction is high (>95%), **DO NOT** conclude slippage is tight. Tell the user: "These trades are execution_mode=paper — entry_price is set directly to signal_mid at fill time. Zero slippage is a mode artifact, not real execution quality. Real slippage can only be measured when (a) a pair is flipped to execution_mode=real, or (b) helio.paper_slippage is wired into the fill path to inject synthetic spread + noise."

5. Highlight any pair with mean slippage >2 pips — that's execution friction worth investigating.

Context: helio/slippage_report.py + helio/paper_slippage.py shipped 2026-04-20. Paper-slippage library exists but isn't wired into runner_unified by default (cohort-reset decision).
