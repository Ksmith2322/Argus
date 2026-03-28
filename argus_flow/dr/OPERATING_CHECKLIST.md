# Argus Operating Checklist
# Created: 2026-03-27
# Updated: 2026-03-28 (monitoring + legacy cleanup + Discord alerts)
# Source: ChatGPT validation charter synthesis

## Pre-Go-Live Monitoring Requirements

### Must be fully operational before real money:
- [ ] Dashboard health bar showing: System status, API connection, valid trades, balance, blocked count
- [ ] Watchdog auto-restarting full fleet (8 FX + 7 futures) with Discord alerts
- [ ] Discord alerts on: runner crash, failed restart, max restarts exhausted
- [ ] Degradation report running daily (automated or manual)
- [ ] Drift report running daily (live vs research comparison)
- [ ] Heartbeat files refreshing every 5 minutes per instrument
- [ ] No legacy scheduled tasks interfering (all disabled/removed)
- [ ] TWS auto-restart configured (not auto-logoff)
- [ ] Maintenance blackout enforced (01:30-04:30 UTC)

### Legacy Systems Cleaned Up (2026-03-28):
- [x] ArgusRefreshCandles — DISABLED (old crypto candle refresh)
- [x] ArgusCohortReport — DISABLED (replaced by degradation_report.py)
- [x] ArgusWeeklyDigest — DISABLED (legacy)
- [x] Argus_Nightly_Backtest — DISABLED (legacy crypto backtest)
- [x] ArgusWatchdog — UPDATED for full fleet (8 FX + 7 futures) + Discord alerts
- [x] Old 3-pair runner config in watchdog — FIXED to use all 8 FX pairs

### Active Scheduled Tasks:
- ArgusWatchdog — RUNNING (auto-restart + Discord alerts)
- ArgusGitBackup — daily git backup (keep)
- ArgusUSBBackup — USB backup (keep)
- ArgusVerifyBackup — backup verification (keep)
- ArgusWiFiEnable — 7:00 AM daily
- ArgusWiFiDisable — 8:00 PM daily

## Daily

1. **Did live behavior stay inside expected band?**
   - Rolling expectancy, PF, WR, timeout rate, friction, side/session mix, give-back
   - If drift: flag, reduce trust, queue revalidation

2. **Did any control fire, and was it helpful?**
   - Drawdown breaker, regime vetoes, correlation vetoes, health-scaler, blackout
   - Log counterfactual outcome for every block

3. **Did any module dominate today?**
   - % daily PnL by module, by top 1-3 trades, by session
   - Concentration alert even on green days

## Every 30 Valid Trades (per module)

4. **Is live still the same process as research?**
   - Live vs research: expectancy, PF, friction, timeout/give-back, side/session
   - If drifting: probation or quarantine, do not scale

5. **Is the module economically robust?**
   - Remove-best-1/3/5, top-10% trade share, session/regime concentration
   - If bursty: quarantine, do not count toward fleet return

6. **Is there a narrower, better edge hiding inside?**
   - Side-only, session-only, regime-only, combinations
   - If yes: reclassify as module, govern separately

## Every 100 Fleet Trades

7. **Which modules still deserve capital?**
   - Review: expectancy, PF, drift, concentration, dependence, blocker economics, integrity
   - Action: promote / keep / narrow / quarantine / kill

8. **Are controls provably helping?**
   - Shadow accounting: blocked savings vs missed gains per veto type
   - Net positive = keep. Net negative = revise. Unknown = experimental only.

9. **Are modules actually diversified?**
   - PnL correlation, drawdown overlap, session overlap, signal overlap
   - High overlap = redundant risk, enforce combined caps

10. **Is allocator ready for live?**
    - Shadow review: equity freshness, sizing caps, signal arbitration, heat caps
    - Sane = move toward capped live. Not sane = keep shadow.

11. **Is a third allocatable module emerging?**
    - If yes: probationary path. If no: accelerate triage or expand scope.

## Every 300 Fleet Trades

12. **Is 100-125% on track?**
    - 4-5 validated contributors, +2.0 pip/trade after friction, no pair >30% PnL
    - Yes = continue scaling. No = expand scope or rotate.

13. **Is 200% discussable?**
    - 3+ allocatable modules, controlled drift, acceptable concentration, working allocator
    - Only if ALL true. Otherwise: research aspiration only.

14. **Is 300% grounded or fantasy?**
    - 4+ modules or second strategy family, mature allocator, no leverage dependence
    - If not true: do not use in planning.

## Promotion Gates

| From | To | Requirements |
|------|----|-------------|
| Inventory | Probationary | Neighborhood + walk-forward + friction + concentration + segmentation |
| Probationary | Anchor | 60+ live trades + drift inside band + stable expectancy + concentration OK |
| Quarantined | Probationary | Specific quarantine reason resolved with NEW evidence |

## Kill Rules (enforced, not optional)

- Live expectancy below threshold for rolling N trades
- Friction kills the edge
- Remove-best-3 reveals non-allocatable concentration
- One session/regime carries result unintentionally
- Repeated runtime integrity issues
- "It might come back" does NOT override demotion

## 100-Trade Review Template

1. Which modules have positive live expectancy after friction?
2. Which modules are drifting from research?
3. Which modules are over-concentrated?
4. Which controls saved money vs cost money?
5. Which modules are too dependent on each other?
6. Is the allocator choosing right in shadow mode?
7. Which candidate becomes next probationary?
8. What gets promoted / narrowed / quarantined / killed?
9. Are we closer to 100/200/300% operational requirements, or just collecting anecdotes?
