# Argus Instrument Tracker
# Single source of truth for what we trade, what works, and what doesn't.
# Updated: 2026-03-29
# Rule: NEVER re-add a killed instrument without new paper proof + positive backtest.

## Grading Scale

| Grade | Meaning | Action |
|-------|---------|--------|
| A | ANCHOR — validated, research-closed, live-proven | Full size, full trust |
| B | PROBATIONARY — strong backtest, collecting live data | Reduced size, monitoring |
| C | COLLECTING — running but unvalidated | Observe only, no fleet contribution |
| D | QUARANTINED — known issues, needs specific fix | Paper/shadow only |
| F | KILLED — proven non-viable with current strategy | DO NOT TRADE until re-proven |

---

## FX Pairs

### EUR/USD — Grade: A (ANCHOR)
- **Strategy:** T4_full_stack, S35/T30/TO75, session 8-14 UTC
- **Research:** 150/150 neighborhood, PF 1.75, walk-forward 5/6, drift curve valid
- **Edge type:** London session drift capture, short-biased
- **Strengths:** Broad plateau, survives 3 pip friction, filters add 2x value
- **Weaknesses:** Longs conditional (morning+choppy only), timeout-dominated
- **Key metric:** Give-back after +10 < 35%
- **Live status:** Collecting toward 60-trade funding gate

### GBP/USD — Grade: B (PROBATIONARY)
- **Strategy:** range_accel, S30/T50/TO90, session 8-20 UTC
- **Research:** 125/125 neighborhood, PF 3.20, walk-forward 4/4, strong shorts
- **Edge type:** London session, short-dominated (+15/trade short vs +4.6 long)
- **Strengths:** Best raw PF of any pair, distributed returns (remove-best-3 still positive)
- **Weaknesses:** Only 30 backtest trades, session boundary not yet tightened
- **Live status:** Collecting data, 3 trades so far

### EUR/JPY — Grade: C (COLLECTING)
- **Strategy:** T4_full_stack, S15/T40/TO30
- **Research:** Sweep best PF 3.94 but only 18 trades — thin sample
- **Note:** 0.97 correlated with GBP/JPY — essentially same trade
- **Action needed:** Full battery before any trust. Consider killing if GBP/JPY is kept.

### GBP/JPY — Grade: C (COLLECTING)
- **Strategy:** T4_full_stack, S15/T25/TO60
- **Research:** Sweep PF 2.42, 29 trades, walk-forward positive
- **Note:** 0.97 correlated with EUR/JPY — only keep one of these
- **Action needed:** Full battery to determine if it adds value beyond EUR/JPY

### CAD/JPY — Grade: C (COLLECTING)
- **Strategy:** T4_full_stack, S15/T25/TO30
- **Research:** Sweep PF 2.06, 20 trades
- **Note:** Part of London cluster (trades same days as GBP/USD, EUR/JPY, GBP/JPY)
- **Action needed:** Battery + overlap analysis before promotion

### AUD/JPY — Grade: C (COLLECTING)
- **Strategy:** T4_full_stack, S20/T20/TO45
- **Research:** Sweep PF 1.56, 58 trades (most trades of any pair)
- **Note:** Asia session pair (22-08 UTC), independent from London cluster
- **Strengths:** High trade count, true diversifier
- **Weaknesses:** Lower PF, 1:1 risk/reward
- **Action needed:** Battery + live validation

### USD/JPY — Grade: D (QUARANTINED)
- **Strategy:** range_accel_NY, S15/T30/TO30
- **Research:** 35/36 neighborhood profitable, PF 1.62
- **WHY QUARANTINED:** Remove-best-3 goes NEGATIVE. Edge is real but carried by 3 outlier wins.
- **Edge type:** Possibly Asia-range-long specialist, not general pair
- **To re-promote:** Need 50+ trades where remove-best-3 stays positive, or find narrower viable module
- **Last reviewed:** 2026-03-27

### AUD/USD — Grade: C (COLLECTING)
- **Strategy:** range_accel_NY, S20/T50/TO90
- **Research:** Sweep PF 1.59, 38 trades
- **Note:** Asia session pair, true diversifier from London cluster
- **Action needed:** Battery before promotion

---

## Futures

### MES (S&P Micro) — Grade: C (COLLECTING)
- **Strategy:** range_accel, S30/T60/TO60
- **Note:** No full battery yet. Futures use delayed data.
- **Action needed:** Validate with real-time data subscription before trusting

### MNQ (Nasdaq Micro) — Grade: C (COLLECTING)
- **Strategy:** range_accel, S30/T60/TO60
- **Same as MES** — needs real-time data and full battery

### MYM (Dow Micro) — Grade: C (COLLECTING)
- **Strategy:** range_accel, S30/T60/TO60
- **Same status as MES/MNQ**

### M2K (Russell Micro) — Grade: C (COLLECTING)
- **Strategy:** range_accel, S30/T60/TO60
- **Same status**

### MGC (Gold Micro) — Grade: C- (WATCH)
- **Strategy:** range_accel, S25/T50/TO60
- **Live results:** 3 trades, 0 wins, -31.7 pips
- **Concern:** All losses. May not suit range_accel. Needs review at 10 trades.
- **Action:** If 0/10 WR by 10 trades, KILL.

### MCL (Oil Micro) — Grade: C (COLLECTING)
- **Strategy:** range_accel, S30/T60/TO60
- **Live results:** 1 trade, +0.7 pips (win)
- **Too early to judge**

### NKD (Nikkei) — Grade: F (KILLED)
- **KILLED:** 2026-03-29
- **Record:** 0 wins / 7 losses, -550 pips
- **Every single trade stopped out**
- **Root cause:** Range_accel doesn't work for Nikkei. Too volatile, gaps through stops.
- **DO NOT RE-ADD** unless: new strategy developed specifically for NKD, paper-tested with positive backtest over 50+ trades, and separate cohort validation.

---

## Fleet Performance Summary

| Instrument | Grade | Trades | Win Rate | Net PnL | Status |
|-----------|-------|--------|----------|---------|--------|
| EUR/USD | A | 1 | 1/1 | +1.0 | Anchor |
| GBP/USD | B | 3 | 1/3 | -19.9 | Probationary |
| EUR/JPY | C | 0 | - | - | Collecting |
| GBP/JPY | C | 1 | 1/1 | +7.5 | Collecting |
| CAD/JPY | C | 1 | 1/1 | +3.4 | Collecting |
| AUD/JPY | C | 3 | 2/3 | +3.1 | Collecting |
| USD/JPY | D | 2 | 0/2 | -10.8 | Quarantined |
| AUD/USD | C | 2 | 0/2 | -17.8 | Collecting |
| MES | C | 0 | - | - | Collecting |
| MNQ | C | 0 | - | - | Collecting |
| MYM | C | 0 | - | - | Collecting |
| M2K | C | 0 | - | - | Collecting |
| MGC | C- | 3 | 0/3 | -31.7 | Watch |
| MCL | C | 1 | 1/1 | +0.7 | Collecting |
| NKD | F | 7 | 0/7 | -550.0 | KILLED |

---

## Rules for This Tracker

1. **Update after every 30-trade checkpoint**
2. **Grade changes require evidence, not feelings**
3. **Killed instruments stay killed for minimum 3 months**
4. **Re-adding requires:** new strategy + 50 trade backtest + positive PF + paper cohort
5. **This file is the single reference** — if it's not here, we don't trade it
