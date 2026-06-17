---
name: Real-Money Boundary — mechanical separation between paper and real
description: Stupid-proof boundary rules. real_money_enabled=false default, strategy allowlist, account validation, order tagging, mismatch detection. The most important safety rule for live capital.
type: project
originSessionId: 22066483-fb20-4851-b090-d78ddc84252a
---
## Why this exists

The biggest real-money risk is not strategy loss. It's **the wrong runner / wrong account / wrong size / wrong strategy accidentally touching real capital**. A bug, a config typo, an environment variable left set, a test account confusion, a copy-paste error — any of these can put real money on something that wasn't meant for real money.

The boundary must be stupid-proof. The bot must default to "everything is paper" and require multiple explicit gates to allow real-money flow.

## Core rules

### Rule 1 — Default off

```python
REAL_MONEY_ENABLED = False  # global, repo-wide default
```

This is set in `helio/real_money.py` (new module). Every runner imports it. Without explicit override, real money cannot flow. Restart of any runner without the override = back to paper.

### Rule 2 — Per-strategy allowlist

```json
// argus_flow/configs/real_money_allowlist.json
{
  "real_money_enabled_strategies": [
    "forge_vix_intraday"
  ],
  "max_strategies_real": 1,
  "ledger_entry_authorizing": "argus_flow/logs/capital_promotion_ledger.jsonl",
  "ledger_entry_id": "abc123-2026-05-30",
  "approver": "ksmith2322",
  "signed_at": "2026-05-30T19:30:00Z"
}
```

Only strategies named in this allowlist may submit real-money orders. Adding a strategy to the allowlist requires:
1. Capital promotion ledger entry justifying it
2. User signed off in the ledger entry
3. `max_strategies_real` cap respected (1 at first, increments only with cohort_report ledger entries)

### Rule 3 — Account ID validation (CONFIRMED: separate accounts)

User clarified 2026-04-26: **paper and real are SEPARATE IBKR accounts.**
- **Paper:** `DUP472829` ($11,815 — QA environment, balance allowed to fluctuate as part of testing)
- **Real:** TBD — separate account, currently ~$50 (minimum to keep account open)

The boundary is **mechanical**: each runner connects to ONE account via the clientId/account-id mapping. Mixing requires deliberate code change, not just an env variable flip. This is the cleanest possible boundary.

Implementation:
- `helio/real_money.py` keeps a hardcoded mapping `{"paper": "DUP472829", "real": "<real_acct_id>"}` populated at config time
- Every order's `trade.account` validated against the strategy's allowlisted account before submission
- A strategy in the paper allowlist that tries to submit with `account=<real>` (or vice versa) raises `AccountBoundaryViolationError` and the order is rejected before transmission

Mental model (per user 2026-04-26):
- Paper account is the **QA phase**. Balance changes are testing, not P&L stories. Reset freely.
- Real account is the **production destination**. Strategies migrate there after passing the readiness gate. Capital is added when strategies are promoted.
- Eventually: every strategy is either real-promoted or killed. No third state.

### Rule 4 — Order tagging

Every order from a real-money-allowed strategy gets tagged at submission:
```python
order.orderRef = f"argus-real-{strategy}-{ledger_entry_id}"
```

The tag includes the ledger entry that authorized it. Untagged orders or orders with malformed tags = mechanical reject. Audit trail traceable from any real fill back to the ledger.

### Rule 5 — Hard dollar cap per submission

```python
REAL_MONEY_MAX_NOTIONAL_PER_ORDER = 5000  # dollar cap, override per ledger entry
```

A single real-money order's notional cannot exceed this cap regardless of what the sizing logic computes. Catches sizing bugs before they hit the wire. Cap is raised manually via ledger entry as account scales.

### Rule 6 — Pre-submission confirmation layer (first 30 days)

For the FIRST 30 days of real-money operation per strategy, every order pauses for explicit user confirmation:

```python
if strategy.real_money_days_alive < 30:
    discord_alert(f"REAL ORDER PENDING: {strategy} {direction} {symbol} {size} — react with ✅ to submit or ❌ to skip")
    decision = wait_for_user_reaction(timeout_s=300)  # 5 min
    if decision != "approve":
        order.skip()
```

After 30 days clean: confirmation drops to daily summary review only. Per user-locked decision: manual approval until 90+ days real-money evidence, then semi-auto.

### Rule 7 — Mismatch detection (fleet-wide)

Daemon runs every 5 min:
```
For each open IBKR position:
  - Match to strategy via order tag
  - If tag missing OR strategy not in allowlist OR account doesn't match real-allowlist:
    - HALT all real-money trading
    - Discord URGENT alert
    - Require manual reconciliation
```

This catches the failure modes:
- Ghost positions (broker shows position, no strategy claims it)
- Wrong-account positions (real strategy submitted to paper, or vice versa)
- Tag-tampered positions (manual fills bypassing the system)

### Rule 8 — Daily real-money report

End-of-day Discord digest specifically for real-money operation:

```
=== REAL-MONEY DAILY REPORT 2026-06-15 ===
Account: U1234567 (real)
Equity start: $5,000.00
Equity end: $5,047.30
Net P&L: +$47.30 (+0.95%)
Strategies active: 1 (forge_vix_intraday)
Trades: 3 (2W/1L)
Reconciliation: clean
Cluster exposure: SHORT_VOL 0.3× (within 0.5× cap)
Drawdown today: -1.1% intraday peak, recovered
Issues: none

=== PAPER FLEET (separate report) ===
[unchanged]
```

Sent ONLY to a DEDICATED real-money Discord channel, separate from paper alerts. Different channel = different visual context = harder to confuse.

## Implementation work — Week 3-4

| Day | Work |
|---|---|
| Wed 5/13 (Week 3) | Build `helio/real_money.py` module with REAL_MONEY_ENABLED + allowlist loader |
| Thu 5/14 | Wire account_id + order tag validation into all 3 executor paths |
| Fri 5/15 | Build mismatch-detection daemon + 5-min cycle |
| Mon 5/18 (Week 4) | Build pre-submission confirmation layer (Discord reaction-based) |
| Tue 5/19 | Daily real-money report + dedicated Discord channel setup |
| Wed 5/20 | Drill: try to submit a real-money order from a non-allowlisted strategy → must reject |
| Thu 5/21 | Drill: inject a wrong-account-id order → must reject |
| Fri 5/22 | Drill: tamper an order tag → mismatch detection must halt + alert |

All drills mandatory before 5/28 readiness gate sign-off.

## What this boundary does NOT do

- **Does not prevent strategy logic errors.** A real-money allowlisted strategy with bad logic can still lose money. That's the strategy scorecard's job.
- **Does not replace user judgment.** The boundary stops most accidents; user judgment stops the rest.
- **Does not handle adversarial cases.** Assumes the threat model is "bug" or "typo" not "attacker." If an attacker has shell access, they can bypass the boundary.

## Locked defaults (user 2026-04-26)

- `real_money_enabled=false` global default
- Manual approval required until 90+ days real-money evidence
- 1 strategy real at first; cap grows only via ledger entries
- Pre-submission confirmation for first 30 days per strategy
- Hard dollar cap per submission ($5K initial, raised via ledger)
- Account separation preferred (mechanical) — confirmed pending user answer

## How to apply this memory

**Why:** capital can be hurt in 1 second by a bug that takes 10 seconds to type. The boundary makes that 1-second-bug path require multiple deliberate overrides.

**How to apply:**
- Week 3-4 implementation is mandatory before any real-money go-live.
- Drills are not optional. If any drill fails, real-money launch defers.
- The dedicated Discord channel is critical — visual separation prevents confusion.
- "Just enable it for testing" is a banned phrase. Any real-money enablement requires a ledger entry + user signature.
