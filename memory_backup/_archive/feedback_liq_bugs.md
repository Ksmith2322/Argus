---
name: Liquidity Filter Bugs
description: Two bugs in liquidity.py / engine.py that make the liquidity score penalty inert in PENALIZE mode
type: feedback
---

## Bug 1: Unit Mismatch in Volume Floor

`LIQ_MIN_VOL_USD_1M=50000` is loaded as `self.min_vol_1m = 50000` but compared against
`vol_1m` which is in ETH units (from candle CSV, typically 25-1062 ETH/min).

Result: 25 ETH < 50000 → `VOLUME_BELOW_MIN` fires on EVERY entry.

Fix: Either:
- Set `LIQ_MIN_VOL_USD_1M=0` and use `LIQ_MIN_VOL_UNITS_1M=25` (ETH units, 25 ETH/min ≈ $50k at $2000)
- OR convert vol_1m×px to USD before comparison in liquidity.py

## Bug 2: Penalty Never Applied in PENALIZE Mode

In `engine.py::_apply_liquidity_overlay_to_confluence()`:
```python
if ok:
    return eff_score, eff_gate, eff_reason, []  # Returns early, skips penalty!
```

In PENALIZE mode, `LiquidityResult.ok = True` always (unless `LIQ_PENALIZE_HARD_BLOCK_AT > 0` fires).
So the early return skips the penalty application block entirely.

Fix: Set `LIQ_PENALIZE_HARD_BLOCK_AT=10` to trigger `ok=False` after 1 violation of 10 pts,
OR restructure the function to apply penalties before the early return.

## Combined Effect

Both bugs exist simultaneously. Effect: liquidity violations are logged in the reason string
(`violations=VOLUME_BELOW_MIN;penalty=-10`) but have ZERO effect on scores or blocking.
The liq filter is effectively inert in PENALIZE mode.

## Action Required

Fix AFTER current baseline backtest completes (backtest running 2026-03-11 22:19, config hash 0bb53dcf171d).
A new baseline is needed after the fix to ensure apples-to-apples comparison.
