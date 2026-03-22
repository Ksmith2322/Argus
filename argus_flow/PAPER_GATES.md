# Paper Trading Acceptance & Kill Gates

Locked before Sunday 2026-03-22 market open. No changes during first week.

## Operational Gates (all three runners)

### PASS criteria (must all hold):
- [ ] Connects to IBKR gateway on startup
- [ ] Receives live market data (bid/ask not -1)
- [ ] Seeds historical bars successfully
- [ ] Writes signals.csv on every evaluation cycle
- [ ] Writes trades.csv on every entry/exit
- [ ] State file updates on every position change
- [ ] No duplicate signals within min_gap window
- [ ] No orphaned positions (entry without eventual exit)
- [ ] Clean shutdown on Ctrl+C (state saved)
- [ ] Clean restart (state restored correctly)

### KILL triggers (any one = stop runner, investigate):
- Repeated connection drops (>3 in 1 hour)
- State file corruption (position mismatch on restart)
- Duplicate entries (two positions open simultaneously)
- Artifacts stop writing for >5 minutes during market hours
- Gateway authentication expires without recovery

---

## Strategy Gates (per instrument)

### EUR/USD (T4 full stack)
**Replay expectations (from 33-day backtest):**
- Signals/day: ~8 (acceptable range: 3-20)
- Win rate: ~54% (acceptable: 40-65%)
- Avg hold: ~45min (most timeout at 60min)
- Stop rate: ~12% | Target rate: ~1.5% | Timeout: ~86%
- Exp/trade: ~+1.0 pip

**KILL if after 30+ trades:**
- Win rate < 35%
- Expectancy < -1.0 pip/trade
- Signal frequency < 1/day or > 30/day
- Stop rate > 40%

### MNQ (vol_burst)
**Replay expectations (from 18-day backtest):**
- Signals/day: ~5.5 (acceptable range: 2-12)
- Win rate: ~53% (acceptable: 38-65%)
- Stop rate: ~3% | Target rate: ~1.5% | Timeout: ~86%
- Exp/trade: ~+6.4 bps (~15 pts at 24000)

**KILL if after 30+ trades:**
- Win rate < 35%
- Expectancy < -5 bps/trade
- Signal frequency < 1/day or > 20/day
- Stop rate > 30%

### GBP/USD (range+accel)
**Replay expectations (from 33-day backtest):**
- Signals/day: ~19 (acceptable range: 8-35)
- Win rate: ~52% (acceptable: 38-60%)
- Stop rate: ~3% | Target rate: ~0.5% | Timeout: ~96%
- Exp/trade: ~+0.9 pip

**KILL if after 30+ trades:**
- Win rate < 35%
- Expectancy < -1.0 pip/trade
- Signal frequency < 3/day or > 50/day
- Stop rate > 30%

---

## Replay-Paper Divergence Check

The most important metric. After first 50 signals per runner:

| Metric | Acceptable Divergence | Investigate If |
|---|---|---|
| Signals/day | ±50% of replay | Outside range |
| Win rate | ±15 points of replay | Outside range |
| Stop rate | ±10 points of replay | Outside range |
| Timeout rate | ±10 points of replay | Outside range |
| Avg hold time | ±50% of replay | Outside range |

If divergence is large but consistent → model assumptions wrong (fixable).
If divergence is erratic → operational bug (investigate immediately).

---

## First Week Rules

1. **No threshold changes.** Run exactly what was backtested.
2. **No new strategies.** Focus on these three only.
3. **No PnL-based decisions.** Judge on operational health + divergence.
4. **Log everything.** Signals, trades, features, state transitions.
5. **Daily check-in.** Review signals/day, any errors, divergence metrics.

---

## Promotion Criteria (after 30+ trades per runner)

A strategy gets promoted to "live micro" only if ALL hold:
- Expectancy positive (even if small)
- Replay-paper divergence within acceptable bands
- No operational issues
- Drawdown within 2x of replay max drawdown
- Consistent signal frequency

## Demotion/Kill Criteria
- Expectancy significantly negative after 30+ trades
- Divergence outside bands with no explanation
- Operational instability
- Signal frequency wildly off (market microstructure mismatch)