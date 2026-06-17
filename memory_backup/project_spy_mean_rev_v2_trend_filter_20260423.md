---
name: spy_mean_rev v2 — trend filter added 2026-04-23
description: First DEGRADED verdict surfaced by operational_maturity (drift 0.257, -$124 in 18 trades on reset day). Root cause: classic RSI(2) whipsaw on trending SPY. Added 50-EMA trend filter to block counter-trend entries. Now v2 strategy, different from backtest.
type: project
originSessionId: 81afc97b-10bf-49fd-ba79-e5a89a7f400b
---
## The problem

2026-04-23 reset day. forge_spy_mean_rev fired 18 trades in ~3.5 hours
(14:30-18:00 UTC). Results:
- 11 stops, 7 targets → WR 38.9%
- All trades duration_min = 5.0 (exit on the very next 5-min bar)
- Net: -$124.42 on $11,815 anchor
- Live PF 0.373 vs backtest 1.45 → drift_ratio 0.257 → DEGRADED

Pattern: SPY dropped ~1% on the day. RSI(2) kept triggering oversold LONGs.
Each trade: price dips → RSI goes <10 → LONG entry → price keeps dipping →
hit stop on next 5m bar. Classic mean-rev-on-trending-day failure mode.

## The fix

Added standard Connors/Alvarez trend filter to `forge/spy_mean_rev/runner.py`:
- `PARAMS["trend_ema_period"] = 50` (50-period EMA on 5m bars ≈ 4hr)
- `PARAMS["trend_filter_enabled"] = True`
- PARAMS version bumped `v1 → v2` (distinguishes new trades from old)

In `signal_check()`:
- Compute `trend_ema = df["Close"].ewm(span=50).mean()`
- If close > ema: trend_bias = "up" (block shorts)
- If close < ema: trend_bias = "down" (block longs)
- Only trade aligned with trend

## What's different from backtest

**v2 is a different strategy from the one whose backtest PF was 1.45.**
- Backtest used v1 (no trend filter)
- v2 will show lower signal count (filter blocks ~30-50% of setups)
- drift_ratio against 1.45 backtest is NOT an honest comparison anymore

**How to apply**: when user asks about spy_mean_rev verdict, note that
post-2026-04-23 trades are v2 and aren't directly comparable to the 1.45
backtest PF. When v2 has 30+ trades, need to re-run the backtest with
trend_filter_enabled=True to get an honest v2 bt_pf for comparison.

## Expected behavior

- Strong trending days: v2 blocks most counter-trend entries → fewer losses
- Rangebound days: similar to v1 (RSI extremes are usually near EMA)
- Volume: ~30-50% fewer trades than v1

## Rollback

If v2 performs worse than v1 after 30+ trades:
- Set `PARAMS["trend_filter_enabled"] = False` in runner.py
- Restart process
- Original v1 behavior restored

## Unresolved

- Backtest re-run with trend_filter_enabled=True needed for honest v2 bt_pf
- Consider 20-EMA or 200-period variants based on v2 performance
