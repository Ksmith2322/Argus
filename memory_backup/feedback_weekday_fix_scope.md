---
name: Weekday fix scope — ops bugs now, strategy tweaks on weekends
description: Operational/infrastructure bugs get fixed during the trading week. Strategy parameter/logic changes defer to weekends, unless a big bug is found.
type: feedback
originSessionId: 0256622d-fcf2-4b57-9505-2a4e805eef61
---
**Rule:** Classify every issue as **operational** vs **strategy** before proposing work.

- **Operational / infrastructure** (fix during the week, real-time):
  - Broker reconciliation drift, RECON_DRIFT
  - Trade recording gaps, dashboard correctness, PnL accuracy
  - Daemon health, heartbeat staleness, restart loops
  - Order routing, RTH gating, OCO bracket cleanup
  - Sizing/cluster/notional cap enforcement
  - Logging, alerting, monitoring infrastructure
  - Anything that prevents *measuring* what the strategies are doing

- **Strategy parameter/logic** (defer to weekend, unless a *big bug*):
  - Entry/exit thresholds, stop/target widths
  - Signal logic, indicator parameters
  - Universe selection, instrument additions
  - Re-tuning, refitting, optimization

**Why:** Operational issues block the validation effort itself — you cannot measure edge if records are wrong, gating is broken, or trades aren't recorded. Strategy tweaks during live trading distort the very performance signal we're trying to measure for the 2026-05-31 freeze decision. Mid-week strategy work also creates uncontrolled experiments with no clean before/after.

**How to apply:**
- When user reports an issue or I notice one, **classify first**, then propose.
- If operational → propose fix, proceed if approved.
- If strategy → log it as a weekend candidate, do not patch mid-week.
- Exception: a *big bug* in strategy logic (wrong direction, double-fire entries, ignoring stops, etc.) is closer to operational — raise it as an exception, get explicit approval to fix mid-week.
- The "weekend" window applies through the 5/31 validation phase. May relax post-freeze.
