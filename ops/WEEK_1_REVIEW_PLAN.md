# Week 1 Review Plan

**Review date:** ~2026-04-18 (Friday)
**Burn-in period:** 2026-04-11 → 2026-04-18

## What We're Looking For

After 7 days of paper trading on real broker (DUP472829), we need to answer:

1. **Did each system actually trade?**
2. **Did backtest performance hold up live?**
3. **What's the slippage cost?**
4. **What broke operationally?**
5. **What needs to be killed, kept, or tweaked?**

## The Process — Run These Commands

### Step 1: Generate the weekly review report
```powershell
cd C:\Argus\repo
C:\Argus\.venv\Scripts\python.exe -m helio.weekly_review --days 7 --dry-run
```
This produces the per-system summary we'll dissect together.

### Step 2: Pull all trades for analysis
```powershell
# Argus FX trades from the week
Get-ChildItem argus_flow/logs/*/trades.csv | ForEach-Object { Get-Content $_ -Tail 50 }

# Stock system trades
Get-Content titan/logs/trades.csv -ErrorAction SilentlyContinue
Get-Content hermes/logs/trades.csv -ErrorAction SilentlyContinue
Get-Content apollo/logs/trades.csv -ErrorAction SilentlyContinue
```

### Step 3: Pull all orders (slippage analysis)
```powershell
Get-Content titan/logs/orders.csv -ErrorAction SilentlyContinue
Get-Content hermes/logs/orders.csv -ErrorAction SilentlyContinue
Get-Content apollo/logs/orders.csv -ErrorAction SilentlyContinue
```

### Step 4: Get the daily snapshots
```powershell
Get-ChildItem argus_flow/logs/fleet_snapshots/snapshot_*.json | Select-Object -Last 7
```

### Step 5: Check operational health log
```powershell
# Restart count this week (should be low or zero)
Select-String "Restarted" argus_flow/logs/fleet_monitor.log | Measure-Object

# Stale events
Select-String "STALE" argus_flow/logs/fleet_monitor.log | Measure-Object

# Discord alert count
Get-Content argus_flow/logs/alert_history.json | ConvertFrom-Json | Measure-Object
```

## The Decision Framework

For each of the 5 systems, compare:

| Metric | Backtest Said | Live Says | Action |
|--------|--------------|-----------|--------|
| **Argus** | PF 1.1-1.3, ~10 trades/wk | ? | If <5 trades or PF <0.8, debug |
| **Titan** | PF 1.59, 1-2 trades/wk | ? | If 0 trades, scanner is broken |
| **Hermes** | PF 0.99 (1.27 filtered), 1-2 trades | ? | If 0 trades all week, lower score threshold OR kill |
| **Apollo** | Tier 1 = 100% drift, 1-2 setups/wk | ? | TSM/NFLX Apr 16 is the test |
| **Ares** | Monthly, no week 1 action expected | ? | First rebalance May 1 |

### Decision Tree per System

**If backtest holds (PF within 30% of expected):**
- KEEP, continue burn-in week 2
- Note any operational issues

**If backtest is 30-50% worse than expected:**
- INVESTIGATE — is it slippage? Bad setups? Market regime?
- Pull individual trades and look at why each one lost
- Consider parameter tweak (only after analysis, not before)

**If backtest is >50% worse OR negative PF:**
- KILL the system
- Document why it failed
- Don't replace it immediately — see what the surviving systems do alone

**If a system had ZERO trades all week:**
- Scanner is broken OR thresholds too tight OR fleet_risk blocking
- Check signals.csv for trigger events vs blocks
- Likely a signal flow bug

## The Key Tests

### Apollo's Big Test: April 16
- **TSM reports** April 16 (Tier 1, 100% beat rate, score 100)
- **NFLX reports** April 16
- If either gaps 6%+ on a confirmed beat, Apollo enters Day 2 (April 17)
- This is the FIRST live test of the post-ER drift strategy
- Watch the entry, the fill quality, the trail stop behavior

### Slippage Test (all stock systems)
- Compare Titan/Hermes/Apollo orders.csv `entry_price` vs the actual fill price
- Backtest assumes perfect fills — reality won't match
- Calculate average slippage per trade
- If slippage eats >10% of expected edge, that's a problem

### Argus Governor Validation
- Argus needs 30+ live trades for governor validation
- After week 1, count trades. Likely 5-15.
- After week 2, target 30+
- Only THEN do we flip governor from LOG_ONLY → GATE mode

## What We'll Build Next (Conditional)

**If everything is healthy:**
- Add ATR-scaled stop tuning per pair (Argus)
- Build a trade quality dashboard (slippage per system)
- Wire economic calendar to Argus NEWS_BLOCKED gate (already built, just needs activation)

**If Apollo's first trade succeeds:**
- Tighten Apollo to Tier 1 only for week 2 (4 stocks: GOOGL, KLAC, PEP, WMT)
- These are the 100% consistency stocks — if drift works, it works hardest here

**If Hermes has zero trades:**
- Either lower threshold from 80 to 70 (more trades, lower quality)
- Or kill Hermes — the edge is too thin to wait on

**If Titan has zero trades:**
- Scanner is the problem — check why USO LONG isn't generating new entries
- Check if the AI overlay is over-blocking

**If Argus PF is good but trade volume is low:**
- Loosen the AI overlay consensus threshold (currently 0.15)
- More trades = faster governor validation

## The Hard Questions to Ask

1. **Is the fleet showing diversification benefit?** Are the systems' PnLs uncorrelated? If Titan and Apollo both have the same ups/downs, we don't have 5 strategies — we have 1.

2. **Is operational complexity worth it?** 5 systems = 5x debugging surface. If only 2 are profitable, kill the other 3 even if they're slightly positive.

3. **What's the worst-case daily drawdown?** Did any single day eat more than 3% of model equity? If yes, position sizing is too aggressive.

4. **Are the kill rules being tripped?** PortfolioGuard, fleet_risk, drawdown_pause — did any of them block trades? If yes, why?

## Expected Realistic Outcomes (Honest)

**Most likely:**
- 5-15 Argus trades, PF 0.9-1.3 (slightly worse than backtest due to slippage)
- 0-2 Titan trades (scanner is selective)
- 0-1 Hermes trades (very rare events)
- 0-1 Apollo trades (depends on TSM/NFLX gap)
- 0 Ares (monthly cadence)
- **Total fleet trades: 5-20**

**Not enough for statistical confidence on Titan/Hermes/Apollo** — those need 4+ weeks. But enough to validate Argus and calibrate expectations.

**Best case:**
- 15+ Argus trades at PF 1.3+
- Apollo TSM/NFLX both gap and we capture the drift
- Titan adds 2-3 stock positions
- Fleet PnL net positive
- Confidence on Argus jumps to 65%, Apollo to 90%

**Worst case:**
- TWS keeps dropping, missed signals
- Slippage destroys edge
- One system blows up, others marginal
- Confidence drops, we kill 2-3 systems

## What I Will NOT Do Friday

- Add new strategies
- Change strategy logic
- Promote to live capital
- Make decisions based on <5 trades per system

We need data, not theater. If a system has 1 trade, we don't know anything yet.

## Bottom Line

Friday's review = **diagnose, don't redesign**. Look at every trade, compare to expectations, identify the gap, decide kill/keep/wait. Then close the laptop and let week 2 run.
