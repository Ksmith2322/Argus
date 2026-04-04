---
name: compare
description: Compare performance across Greek family strategies on the same pair. Usage /compare AUDJPY
allowed-tools: Bash Read
argument-hint: '[symbol]'
---

Compare how different strategies perform on $ARGUMENTS:

1. **Run backtests** for the symbol across all applicable strategies:
   - Argus (drift capture) — if FX pair
   - Apollo (mean reversion) — if FX pair
   - Helio (swing trend) — if futures/ETF
   - Hermes (momentum) — if Gold/futures

2. **Show side-by-side**: strategy, trades, WR, PF, total PnL, max DD, timeout rate

3. **Identify**: which strategy works BEST on this specific instrument

4. **Check regime**: what's the current regime for this instrument and which strategy should be prioritized?

5. **Recommendation**: "For $SYMBOL, focus on [strategy] because..."
