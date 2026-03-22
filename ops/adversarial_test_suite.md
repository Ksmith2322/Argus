# Argus Adversarial Test Suite — Kitchen Sink Edition

Every failure mode a trading system can hit in production. Goal: prove the system
fails SAFELY, not that it never fails.

## Category 1: DATA FEED ATTACKS

### 1.1 Feed goes dead (stale data)
- Freeze price feed for 30s, 60s, 5min, 15min
- Expected: STALE_TICK detection triggers, system pauses entries
- Kill condition: system trades on stale data

### 1.2 Feed gap then resume
- Remove 5 minutes of candles from middle of dataset
- Expected: system detects gap, doesn't compute indicators on broken window
- Kill condition: indicators produce garbage values, false signals fire

### 1.3 Duplicate candles
- Insert same candle 2-3 times in a row
- Expected: system deduplicates or handles gracefully
- Kill condition: double-counted volume, inflated indicators

### 1.4 Out-of-order timestamps
- Swap 2-3 candles so timestamps go backwards
- Expected: system detects and rejects or reorders
- Kill condition: silent corruption of rolling calculations

### 1.5 Future-dated candles
- Insert a candle with timestamp 1 hour ahead
- Expected: detected and rejected
- Kill condition: look-ahead contamination

### 1.6 Zero-value candles
- Inject candles with price=0, volume=0, or both
- Expected: filtered out, no division by zero
- Kill condition: crash, NaN propagation, infinite values

### 1.7 Negative prices
- Inject candle with negative price (happened to oil futures in 2020)
- Expected: rejected or handled
- Kill condition: position sizing goes haywire

### 1.8 Price stuck (exchange halted)
- 100 identical candles in a row (O=H=L=C, zero range)
- Expected: compression detector stays sane, no false breakouts
- Kill condition: breakout fires on first normal candle

## Category 2: PRICE SHOCK EVENTS

### 2.1 Flash crash — 5% drop in 60 seconds
- Sharp downward spike, partial recovery
- Expected: stop loss triggers, system doesn't re-enter during chaos
- Kill condition: tries to buy the dip during freefall

### 2.2 Flash crash — 10% drop in 30 seconds
- Extreme event (exchange glitch, whale dump)
- Expected: circuit breaker fires, kill switch consideration
- Kill condition: opens position into the crash

### 2.3 Flash pump — 5% spike in 60 seconds
- Sudden upward spike
- Expected: doesn't chase, cooldown prevents FOMO entries
- Kill condition: buys at the top of the spike

### 2.4 V-shape recovery
- 5% crash followed by full recovery within 10 minutes
- Expected: if in position, doesn't panic sell at bottom then rebuy top
- Kill condition: whipsaw loss on both legs

### 2.5 Slow bleed — 2% over 4 hours
- Gradual decline, no sharp moves
- Expected: time-stop exits, regime shifts to TREND_DOWN, entries blocked
- Kill condition: holds through entire bleed, max drawdown hit

### 2.6 Gap up/down (exchange reopens after maintenance)
- 3% price gap between consecutive candles
- Expected: stop loss triggers at gap price (not original stop level)
- Kill condition: stop loss never triggers, unlimited loss

### 2.7 Cascade liquidation pattern
- Staircase down: drop 1%, pause, drop 1%, pause, drop 1%
- Expected: doesn't interpret each pause as support
- Kill condition: buys each pause thinking bottom is in

## Category 3: VOLUME / LIQUIDITY ATTACKS

### 3.1 Volume spike with no price move
- 100x normal volume, price unchanged
- Expected: liquidity filter doesn't false-positive, delta stays sane
- Kill condition: treats volume spike as breakout confirmation

### 3.2 Volume disappears
- Volume drops to near-zero for 30 minutes
- Expected: liquidity penalty blocks entries
- Kill condition: trades into zero-liquidity market

### 3.3 One-sided volume (all buys, no sells)
- Extreme delta imbalance
- Expected: OB imbalance flags it, system is cautious
- Kill condition: blindly follows one-sided flow

### 3.4 Wash trading pattern
- High volume, tight range, alternating buy/sell in equal sizes
- Expected: compression detector sees it as range, no breakout signal
- Kill condition: false breakout on wash-traded market

### 3.5 Spread blow-out
- Spread widens from 1bps to 100bps suddenly
- Expected: entry blocked by MAX_ENTRY_SPREAD_BPS
- Kill condition: enters with massive spread cost

## Category 4: ORDER BOOK ATTACKS

### 4.1 Spoofed wall (large bid disappears)
- Large bid wall appears, OB imbalance goes bullish, wall pulled
- Expected: if OB imbalance triggered entry, system has stop in place
- Kill condition: relies entirely on OB without confirmation

### 4.2 Thin book both sides
- Order book nearly empty on both bid and ask
- Expected: OB imbalance unstable, system doesn't trust it
- Kill condition: enters based on meaningless imbalance in thin book

### 4.3 OB imbalance flip-flop
- Imbalance alternates +0.5 / -0.5 every second
- Expected: smoothed or ignored, doesn't cause rapid signal changes
- Kill condition: generates contradictory signals on every tick

## Category 5: EXECUTION FAILURES

### 5.1 Fill at worse price than expected
- Slippage 10x normal
- Expected: logged, factored into P&L, alerts if anomalous
- Kill condition: silently eats the slippage, P&L doesn't reflect it

### 5.2 Partial fill
- Only 30% of order fills
- Expected: position sizing accounts for partial, rest handled
- Kill condition: system thinks full position, risk is wrong

### 5.3 Fill timeout
- Order placed but no fill response for 60 seconds
- Expected: system handles gracefully, doesn't place duplicate order
- Kill condition: duplicate orders, double position

### 5.4 Fill arrives after position closed
- Late fill notification from exchange
- Expected: reconciliation catches it on restart
- Kill condition: ghost position, untracked exposure

### 5.5 Exchange rejects order
- API returns error (insufficient funds, invalid pair, maintenance)
- Expected: logged, no state corruption, retry with backoff
- Kill condition: system stuck in OPEN state with no position

### 5.6 API rate limit hit
- Exchange returns 429 (rate limited)
- Expected: exponential backoff, no data gaps
- Kill condition: rapid retry loop, ban from exchange

## Category 6: STATE / RECOVERY ATTACKS

### 6.1 Kill during BUY order
- Process killed after order placed, before fill confirmed
- Expected: restart recovers from fills.csv, reconciles position
- Kill condition: orphaned order, unknown position state

### 6.2 Kill during SELL order
- Process killed after sell placed, before flat confirmed
- Expected: restart detects open position or completed sell
- Kill condition: position stuck in OPEN, never exits

### 6.3 Corrupted snapshot file
- Modify runtime_state JSON to invalid values
- Expected: falls back to canonical truth (fills.csv, positions.csv)
- Kill condition: trusts corrupt snapshot, wrong state

### 6.4 Missing snapshot file
- Delete runtime_state entirely
- Expected: cold recovery from fills.csv
- Kill condition: assumes FLAT when actually in position

### 6.5 Snapshot from different coin
- Swap ETH snapshot into BTC runner directory
- Expected: detected (symbol mismatch), rejected
- Kill condition: trades BTC with ETH state

### 6.6 Clock rollback
- System clock jumps backward 1 hour
- Expected: detects non-monotonic time, handles gracefully
- Kill condition: cooldowns reset, duplicate signals, re-entry

### 6.7 Disk full
- No space to write artifacts
- Expected: detected, system pauses rather than losing data
- Kill condition: silent data loss, corrupt CSVs

### 6.8 Double runner instance
- Two instances of runner_live.py for same coin
- Expected: detected via lock file or position conflict
- Kill condition: both place orders, double exposure

## Category 7: REGIME / INDICATOR EDGE CASES

### 7.1 Perfect sideways market (ATR = 0)
- 1000 candles with identical OHLC
- Expected: ATR-based stops handle gracefully, no division by zero
- Kill condition: infinite position size, zero stops

### 7.2 Extreme volatility regime
- ATR 10x normal, every candle 2% range
- Expected: vol sizing reduces position, wider stops
- Kill condition: normal-sized position in extreme vol = huge risk

### 7.3 Regime rapid oscillation
- TREND_UP → RANGE → TREND_DOWN → RANGE every 5 minutes
- Expected: penalties stabilize, no signal whipsaw
- Kill condition: opens and closes rapidly, fee death by 1000 cuts

### 7.4 All indicators disagree
- MA cross says BUY, regime says TREND_DOWN, OB says bearish, trendlines say support
- Expected: confluence score reflects mixed signals, likely blocked
- Kill condition: takes trade based on one indicator ignoring others

### 7.5 Indicator NaN propagation
- One indicator returns NaN, does it poison all downstream?
- Expected: NaN isolated, other indicators still valid
- Kill condition: entire decision chain returns NaN, no entry ever again

### 7.6 Integer overflow in counters
- Run for 10M ticks — do trade counters, event IDs, or sequences overflow?
- Expected: handled (Python ints don't overflow, but check file sizes)
- Kill condition: artifact files grow unbounded, disk fills

## Category 8: MULTI-COIN INTERACTION

### 8.1 Correlated crash (all coins dump simultaneously)
- ETH and BTC drop 5% at the same time
- Expected: CROSS_COIN_MAX_OPEN prevents doubling down
- Kill condition: opens positions on both coins into crash

### 8.2 One coin feed dies, other continues
- ETH feed stops, BTC keeps going
- Expected: ETH runner detects stale, BTC unaffected
- Kill condition: ETH state corrupts BTC decisions via BTC_LAG_SIGNAL

### 8.3 Divergent coins
- ETH pumps 3%, BTC dumps 3% simultaneously
- Expected: BTC_LAG_SIGNAL detects divergence, cautious
- Kill condition: buys ETH assuming BTC will follow, buys BTC assuming dip

## Category 9: ECONOMIC / CALENDAR EVENTS

### 9.1 FOMC announcement pattern
- Low vol → sudden spike → reversal → trend
- Expected: session modifiers + vol sizing handle it
- Kill condition: enters during spike, stopped on reversal

### 9.2 Weekend low liquidity
- Saturday 3am UTC, 90% volume reduction
- Expected: session OFF penalty + liq penalty blocks
- Kill condition: enters with massive spread in dead market

### 9.3 Exchange maintenance window
- No data for 2 hours, then resume
- Expected: STALE_TICK detection, clean restart when data returns
- Kill condition: calculates indicators on pre-maintenance data

## Category 10: ADVERSARIAL / MALICIOUS

### 10.1 Man-in-the-middle price manipulation
- Feed shows price 2% higher than reality
- Expected: can't fully detect, but execution at real price + slippage logging catches it
- Kill condition: trusts manipulated feed, enters at fake level

### 10.2 API response tampering
- Balance endpoint returns $0 (when actual is $500)
- Expected: safety check, doesn't panic-close positions
- Kill condition: thinks it's bankrupt, emergency sells

### 10.3 Replay attack (old data injected)
- Yesterday's candles served as today's
- Expected: timestamp validation catches it
- Kill condition: trades on stale data thinking it's live

---

## Implementation Plan

### Tier 1 — Build first (highest value, fastest to implement)
1. Flash crash injection (2.1, 2.2, 2.3)
2. Feed gap and stale data (1.1, 1.2)
3. Zero/extreme values (1.6, 1.7, 7.1)
4. Kill mid-trade (6.1, 6.2) — extend Phase 10 tests
5. Volume anomalies (3.1, 3.2)

### Tier 2 — Build second (important, moderate effort)
6. Corrupt/missing state files (6.3, 6.4, 6.5)
7. Regime oscillation (7.3)
8. OB spoofing (4.1, 4.3)
9. Multi-coin correlated crash (8.1, 8.2)
10. NaN propagation (7.5)

### Tier 3 — Build when stable (edge cases, lower probability)
11. Duplicate candles / out-of-order (1.3, 1.4, 1.5)
12. Execution failures (5.1-5.6)
13. Clock manipulation (6.6)
14. Calendar events (9.1-9.3)
15. Adversarial/malicious (10.1-10.3)

---

## Test Harness Design

Each test needs:
1. **Synthetic data generator** — injects specific failure into real candle data
2. **Expected behavior spec** — what SHOULD happen
3. **Assertion checker** — verifies system behaved correctly
4. **Pass/fail report** — machine-readable results

Output: `ops/logs/adversarial/test_<name>_<timestamp>.json`

```json
{
  "test_name": "flash_crash_5pct_60s",
  "injection_point": "candle_index_5000",
  "expected_behavior": "stop_loss_triggered",
  "actual_behavior": "stop_loss_triggered",
  "pass": true,
  "details": {
    "entry_price": 2100.00,
    "stop_price": 2058.00,
    "exit_price": 2055.50,
    "slippage_bps": 12,
    "drawdown_pct": 2.1
  }
}
```