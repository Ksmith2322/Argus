# PROMOTION PIPELINE — Complete Stage Lifecycle
# Last updated: 2026-03-31
# Maintained to match the stage engine in fleet_registry.py, deployment_pipeline.py,
# promotion_gate_v2.py, and demotion_check.py.

---

## STAGE DEFINITIONS

| Stage | Purpose | Risk Sizing | Execution |
|-------|---------|-------------|-----------|
| DISCOVERY | Identify candidates (future) | None | No runner |
| WATCHER | Prove walk-forward viability | None (observe only) | Signals logged, no trades |
| QA | Prove live trading edge | 0.5% base on $10K model, shared earned ladder to 3.0% cap | Paper trades, full governance |
| PROD | Generate returns | 0.5% base on real equity, shared earned ladder to 3.0% cap | Real money, full governance |
| QUARANTINED | Under review, reduced activity | 0.25% (half of base) | Real money, reduced size |
| KILLED | Retired from the managed fleet | None | Non-launchable, excluded from active ops surfaces |

---

## STAGE 1: WATCHER

### Entry criteria (Discovery -> Watcher):
- Discovery intake is registry-driven via managed config metadata or onboarding automation.
- Walk-forward backtest exists with >= 6 folds
- At least 60% of scored folds have positive expectancy
- Backtest covers >= 14 days of data
- Payoff contract defined (target reach, timeout, stop rates)
- Cost model documented (RT fees, spread, slippage)

### Minimum residency:
- 7 calendar days minimum
- Must observe >= 50 signals during residency

### Stay criteria (checked nightly):
- Walk-forward status = PASS, WATCH, or COLLECTING (FAIL = retire to KILLED)
- No data quality issues (gaps > 4 hours during session)
- Signal frequency >= 1/day average over residency

### Promotion to QA:
- Walk-forward status = PASS (>= 60% positive folds)
- Minimum 7 days in WATCHER
- >= 50 observed signals
- No active governance KILL flags
- Historical data file exists and is current (< 7 days old)

### Auto-promotion: YES
- Nightly cohort report checks criteria
- If all met, stage advances to QA automatically

---

## STAGE 2: QA (Paper Trading)

### Entry criteria (Watcher -> QA):
- All WATCHER promotion criteria met
- Config hash frozen at entry (any change = cohort reset)
- Paper model initialized at $10,000 with 0.5% base risk per trade
- QA uses the same earned-risk ladder as PROD; only the equity source differs

### Minimum residency:
- 60 valid trades minimum (was 30 — increased for statistical confidence)
- Minimum 14 calendar days
- Must span at least 2 different market regimes (trending + ranging)

### Stay criteria (checked nightly):
- Config hash unchanged since cohort start
- Invalid trade rate < 10%
- No KILL flags from kill discipline
- Signal frequency ratio vs replay: 0.50 to 1.50
- Drawdown < 3x model drawdown (paper)

### Performance gates for promotion (ALL must pass):

| Gate | Threshold | Rationale |
|------|-----------|-----------|
| Valid trades | >= 60 | Statistical confidence ~75% |
| Positive expectancy | exp - 0.3 pips > 0 (after friction) | Must beat costs |
| Win rate vs replay | abs(live WR - replay WR) <= 15pp | Strategy behaving as modeled |
| Profit factor | >= 1.10 | Positive edge after costs |
| Session concentration | No session > 40% of total PnL | Not a single-session trick |
| Outlier dependency | No trade > 25% of total PnL | Not dependent on one lucky trade |
| Max drawdown vs WF | live DD <= WF DD x 1.25 + 2.0 pips | Within model bounds |
| Regime diversity | Trades in >= 2 regimes | Not a single-regime trick |
| Give-back | Peak-to-current <= 35% of peak PnL | Not giving back gains |
| Consecutive losses | No 8+ consecutive losses | Not in spiral |
| Artifact integrity | CLEAN | Data pipeline trustworthy |
| Kill discipline | PASS | No kill triggers active |

### Promotion to PROD:
- All 12 gates above PASS
- Minimum 60 valid trades AND 14 calendar days
- Walk-forward still PASS on latest data
- Auto-promotes when all criteria met

### Auto-promotion: YES
- Nightly cohort report checks all gates
- On PROMOTE verdict: auto-materialize live config at 0.5% base risk

---

## STAGE 3: PROD (Real Money)

### Entry criteria (QA -> PROD):
- All QA promotion criteria met
- Live config materialized with 0.5% base risk
- Broker connection verified (smoke test PASS)

### Risk sizing progression:

| Milestone | Risk % | Trigger |
|-----------|--------|---------|
| Entry | 0.5% | Auto on promotion |
| 30 live trades, PF >= 1.20 | 0.75% | Auto |
| 60 live trades, PF >= 1.20 | 1.0% | Auto |
| 100 live trades, PF >= 1.30 | 1.5% | Auto |
| 200 live trades, PF >= 1.30, DD < 2x model | 2.0% | Auto |
| 300 live trades, PF >= 1.35, DD < 2x model | 3.0% (earned cap) | Auto |

The same ladder is used in QA and PROD. Stage changes do not change the ladder logic, only the equity base.
Risk step-ups are one-way within a cohort. Demotion/quarantine resets risk back down.

### Stay criteria (checked nightly):
- Kill discipline PASS
- Drawdown < 2x model drawdown (PROD threshold, tighter than QA's 3x)
- Execution delta <= 25% vs paper expectancy
- Signal frequency ratio vs replay: 0.50 to 1.50
- Slippage within 2x modeled spread

### Demotion triggers:

| Trigger | Action | Auto? |
|---------|--------|-------|
| Expectancy drops 20% vs QA baseline | QUARANTINE (watch, not instant kill) | YES |
| 3 consecutive negative weeks (>= 10 trades/week) | QUARANTINE | YES |
| Drawdown > 2x model | QUARANTINE | YES |
| Drawdown > 3x model | KILL | YES |
| Execution delta > 25% vs QA expectancy | QUARANTINE | YES |
| Slippage > 3x modeled spread | QUARANTINE | YES |
| Single-day loss > 5% of account | QUARANTINE | YES |

### Auto-demotion: YES
- Nightly cohort report checks all triggers
- QUARANTINE = reduced sizing, under review
- KILL = permanent retirement

---

## STAGE 4: QUARANTINED (Under Review)

### Entry:
- Any PROD demotion trigger fires
- Risk immediately drops to the fixed quarantine level of 0.25%
- Discord alert sent: "PAIR quarantined: {reason}"

### Residency:
- Minimum 10 trades at reduced sizing
- Maximum 14 calendar days

### Outcomes:

| Outcome | Criteria | Action |
|---------|----------|--------|
| REINSTATE to PROD | 10+ trades, PF >= 1.10, expectancy positive, drawdown recovered | Auto-promote back to PROD at base risk (0.5%), sizing progression resets |
| DEMOTE to QA | 10+ trades but performance still negative | Retire the live artifact; the paper source remains in QA and must re-earn PROD |
| KILL | 14 days elapsed with no improvement OR drawdown > 3x model | Permanent kill |

### Auto-resolution: YES
- Checked nightly
- Quarantine cannot last more than 14 days — must resolve

---

## STAGE 5: KILLED

### Entry:
- Kill discipline fires (3 consecutive negative weeks, 3x DD, payoff contract fail)
- Quarantine timeout (14 days with no improvement)
- Manual kill

### Rules:
- Config is marked non-launchable in deployment_registry.json and removed from active launch/alert/monitor sets
- Runner is stopped on the next managed fleet reconciliation
- Re-entry/cooling-off remains an operational policy; it is not yet auto-enforced in code

---

## DEMOTION PATHS (complete)

```
PROD --[20% exp drop / 2x model DD / execution degradation]--> QUARANTINE --[recovered]--> PROD (base risk)
                                                                         --[still bad]--> live artifact retired; paper source remains QA
                                                                         --[14d timeout / 3x DD]--> KILLED

QA --[config changed]--> QA (cohort reset, trade counter = 0)
QA --[KILL flag]--> KILLED
QA --[WF regresses to FAIL]--> WATCHER

WATCHER --[PASS + 7d + 50 signals + fresh data + no KILL]--> QA
WATCHER --[WF = FAIL]--> KILLED
```

---

## RISK SIZING SUMMARY

| Stage | Equity Base | Risk Per Trade | Max Concurrent Positions |
|-------|-------------|---------------|--------------------------|
| WATCHER | N/A | N/A (no trades) | 0 |
| QA | $10,000 modeled | 0.5% base, earned ladder to 3.0% | Per config session limits |
| PROD entry | Real account | 0.5% | Per config session limits |
| PROD scaled | Real account | Up to 3.0% earned | Per config session limits |
| QUARANTINED | Real account | 0.25% (half base) | Per config session limits |

---

## CORRELATION LIMITS (applies across all PROD pairs)

| Group | Max Concurrent | Rule |
|-------|---------------|------|
| USD pairs (EUR/USD + GBP/USD + AUD/USD) | 2 | Count as 1.5 systems |
| JPY crosses (EUR/JPY + AUD/JPY + CAD/JPY + GBP/JPY) | 2 | Shared JPY risk |
| US equity indices (MES + MNQ + MYM + M2K) | 1 | Highly correlated |
| Uncorrelated | No limit | Independent risk |

---

## GOVERNANCE CADENCE

| Check | Frequency | Scope |
|-------|-----------|-------|
| Managed truth refresh | Every 10 minutes | Canonical governance + oversight chain |
| Kill discipline | Every 10 minutes | All QA + PROD |
| Promotion gate | Every 10 minutes | All QA |
| Demotion check | Every 10 minutes | All PROD |
| Quarantine review | Every 10 minutes | All QUARANTINED |
| Walk-forward refresh | Every 10 minutes for active managed runners | WATCHER + QA + PROD candidates |
| Evidence registry | Every 10 minutes | All managed runners |
| Artifact divergence | Every 10 minutes | All managed runners |
| Position monitor | Every 5 minutes | All launch-enabled managed runners |
| Risk oversight | Every 5 minutes | All launch-enabled managed runners |
| Discord summary | Nightly summary run | Fleet-wide |

---

## IMPLEMENTATION STATUS

| Component | Status | Notes |
|-----------|--------|-------|
| Watcher -> QA auto-promote | BUILT | Via walkforward_validation + deployment_pipeline |
| QA -> PROD auto-promote | BUILT | Via promotion_gate_v2 + deployment_pipeline |
| PROD -> QUARANTINE auto-demote | BUILT | Via demotion_check + deployment_pipeline |
| QUARANTINE -> PROD/QA/KILLED | BUILT | QA path retires the live artifact and leaves the paper source in QA |
| Risk sizing progression | BUILT | Shared ladder across QA + PROD, 0.5% -> 0.75% -> 1.0% -> 1.5% -> 2.0% -> 3.0% |
| 60-trade threshold | BUILT | promotion_gate_v2 is the canonical 60-trade authority |
| Regime diversity gate | BUILT | entry_regime is now journaled on new cohort trades; old cohorts may still show advisory skips until reset |
| Give-back gate | BUILT | Enforced in promotion_gate_v2 |
| Consecutive loss gate | BUILT | Enforced in promotion_gate_v2 |
| Dashboard truth gate | BUILT | promotion_gate_v2 compares local journal counts + stage against /api/ibkr_fleet |
| Correlation limits enforcement | PARTIAL | Guard exists, not yet promoted to one canonical governance artifact |
| Watcher observe-only execution | BUILT | Watcher runners log signals and do not open trades |
| Managed truth refresh | BUILT | refresh_managed_truth drives launch_fleet, watchdog, scheduler, and nightly summary from one path |
