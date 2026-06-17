---
name: Penalty Attribution & MFE/MAE Analysis (2026-03-20)
description: Live analysis proving governor is correctly blocking — missed signals would have lost money. TREND_UP underperformance discovered as new anomaly. Argus Spot shifts to passive observation.
type: project
---

## Key Finding: Governor Is Protecting Capital

Analyzed top 50 near-miss MISSED_BUY_WATCH_GATED episodes (72h window, 30s dedup).

### Headline Numbers
- 36% simple win rate (MFE > MAE) — not enough
- Avg MAE (0.36%) > Avg MFE (0.28%) — losers bigger than winners
- 1% TP / 0.5% SL test: 11.1% win rate, -0.33% expectancy per trade
- 16/50 hit stop-loss, only 2/50 hit take-profit

### Verdict: Blocking was correct. Do NOT lower thresholds.

---

## Surprising Finding: TREND_UP Worse Than RANGE

| Regime | Win Rate | Avg MFE | Avg MAE | SL Hit Rate |
|--------|----------|---------|---------|-------------|
| RANGE | 44.4% | 0.24% | 0.21% | 11.1% |
| TREND_UP | 31.2% | 0.31% | 0.45% | 43.8% |

TREND_UP entries had nearly half hitting stop-loss. March 19 03:xx ETH entries during "TREND_UP" were into a falling knife.

### Likely cause: TREND_UP classification is LAGGING
- Detecting trend PRESENCE, not trend ENTRY OPPORTUNITY
- By the time regime flips to TREND_UP, move is already mature/extended
- Entries are chasing, not catching

### Investigation needed (low priority, after Cascade):
1. Entry timing within TREND_UP (early/mid/late)
2. Distance from local expansion at entry
3. Pullback quality (winners from pullback-hold vs losers from chase)
4. Regime classification latency (how many bars after real transition)

---

## Penalty Attribution Summary (72h)

### ETH (27,070 events, 0 trades)
- Avg base score: 55.6, closest misses had base 97-99
- RANGE penalty: 75.7% of events
- Session penalty: 81.4%
- Liquidity penalty: 54.6%
- OB negative: 40.8%
- Sole blocker on top misses: RANGE (-15)

### BTC (24,929 events, 0 trades)
- Avg base score: 46.1
- Liquidity penalty: **99.6%** — effectively a permanent block
- RANGE: 83.2%
- BTC on Coinbase is not being "evaluated" — it's being pre-rejected
- Either recalibrate liq floor for Coinbase BTC, or acknowledge venue mismatch

---

## Decisions Confirmed

1. **Do NOT lower thresholds** — data proves blocks are correct
2. **Do NOT relax penalties** — more trades would be negative EV
3. **Do NOT overreact to 2 standout winners** (ETH +1.23%, +1.11%) — statistically insufficient
4. **Argus Spot → passive observation** (instrumented research engine, not deployment candidate)
5. **Cascade → primary focus** for Monday
6. **BTC: not yet decisionable** — small sample, liq veto distorts observable data

## Scripts Created
- `ops/analyze_missed_signals.py` — penalty attribution report
- `ops/analyze_missed_mfe_mae.py` — post-miss MFE/MAE counterfactual analysis
