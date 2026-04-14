# PDT Rule Elimination — SEC Approved April 14, 2026

**Status:** APPROVED by SEC (SR-FINRA-2025-017)
**Approval Date:** April 14, 2026
**Effective Date:** ~45 days after FINRA publishes Regulatory Notice + 18-month broker phase-in
**Expected Go-Live:** Late May / Early June 2026

## What's Gone
- Pattern Day Trader label
- $25,000 minimum equity requirement
- 4+ day trades in 5 business days restriction
- Day-trading buying power calculations

## What Replaces It
- Real-time intraday margin standards
- Risk-based controls at broker level
- Similar to existing maintenance margin but applied intraday

## Impact on Helio Fleet

### Stock Systems Unlocked
| System | Current Constraint | After PDT Removal |
|--------|-------------------|-------------------|
| Titan | 2-day minimum hold | Same-day entry/exit on breakouts |
| Apollo | 2-day PDT hold (position_rules.py line 196) | Exit Day 1 post-ER if target hit |
| Hermes | Overnight gap holds | Close same session after gap fade |

### Code Changes Needed (one session)
1. `apollo/ops/trade_manager.py` line ~196: Remove `if days_held < 2: continue`
2. `titan/runner.py`: Remove PDT hold constraint
3. `hermes/runner.py`: Remove minimum hold period
4. All three: Add optional `--intraday` flag for same-day exits

### Volume Impact
- Current (with PDT): ~10-15 stock trades/month across Titan/Apollo/Hermes
- After PDT removal: ~30-50 stock trades/month (2-4x increase)
- Faster exits = less overnight risk = tighter stops = better R:R

### Timeline
- NOW: Continue burn-in as-is (paper trading ignores PDT anyway)
- FRIDAY: Review burn-in results
- LATE MAY: FINRA publishes effective date
- WHEN EFFECTIVE: Strip PDT constraints, recalibrate for intraday exits
- 18-MONTH WINDOW: Some brokers may lag — check IBKR specifically

## Action Items
- [ ] Monitor FINRA Regulatory Notice for exact effective date
- [ ] Check IBKR announcement for their implementation timeline
- [ ] Strip PDT holds from Apollo, Titan, Hermes
- [ ] Backtest Titan/Apollo with intraday exits to see improvement
- [ ] Consider adding more stock-based strategies (no longer PDT-limited)
