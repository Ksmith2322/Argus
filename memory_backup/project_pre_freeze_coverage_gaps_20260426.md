---
name: Pre-freeze coverage gap analysis — what's missing from the fleet
description: Honest inventory of asset classes / timeframes / strategy types not covered by the current 22-strategy fleet. Identifies 2-3 candidates worth adding before 5/31 freeze, plus things to deliberately skip.
type: project
originSessionId: 22066483-fb20-4851-b090-d78ddc84252a
---
## What the fleet covers today

| Category | Covered | Strategies |
|---|---|---|
| FX | USDJPY, GBPUSD, CADJPY, AUDUSD | argus×3, aud_asian_breakout, jpy_pm_short, wick_gbpusd |
| US ETFs | SPY, QQQ, IWM, GLD, GDX, UVXY | multi_orb, spy_mean_rev, vix_intraday, gld_pm_long, gdx_gld |
| US stocks | scanner-driven (broad universes) | titan (~10 trend), apollo (113 ER), hermes (gap-fill liquid) |
| Micro futures | MNQ, MYM | nq_overnight, nq_london_close, mamba, tori, cuebanks |
| International ETFs | EEM, EWJ, VGK, EFA, FXI, INDA | tom_international |
| Sector ETFs | rotation universe | ares |
| Event-driven | FOMC, S&P add, congressional | fomc_drift, rebalance, themis |
| Cross-asset | GDX/GLD pair only | gdx_gld |

## Real gaps (in priority order of "worth adding before 5/31")

### Tier 1 — Worth seriously considering (would fill genuine asset-class gaps)

**1. Bond / Treasury futures (ZF or ZN momentum)**
- Why: entire asset class missing. Bonds move inversely to equities on rate news; provides real diversification, not just instrument expansion.
- Cost: port titan/mamba's trend-following logic to ZF (5-yr) or ZN (10-yr). Small contract size, IBKR paper supports.
- Time: 4-6 hours of code + 2-week observation = doable by 5/15.
- Value: even 1 working bond strategy materially diversifies fleet.

**2. Short-volatility strategy on UVXY/VIXY**
- Why: vix_intraday and vix_revert are LONG vol. Long vol bleeds in calm markets (~80% of the time). Short vol earns the calm-market premium. Inverse exposure balances the fleet.
- Cost: literally inverse of vix_intraday. Same data, different signal direction. Pattern is "VIX spikes → fade the spike." Equivalent edge if SHORT_UVXY_AFTER_SPIKE pattern works.
- Time: 2-4 hours of code (mostly copy-paste-invert).
- Risk: short-vol losses are asymmetric (one bad spike eats months of gains). Must size small + use hard stops.
- Value: covers the calm-market regime where long-vol bleeds.

**3. SPY/TLT or SPY/IEF ratio mean-reversion (equity-vs-bond pair)**
- Why: gdx_gld is the only relative-value play. Adding an equity/bond pair captures a different macro relationship — "stocks vs bonds rotation" is one of the most-traded relative-value setups.
- Cost: port gdx_gld_runner.py, change instruments + parameters.
- Time: 4-6 hours.
- Value: meaningful new edge if it works; failure is informative (says equity/bond correlation isn't tradeable at this scale).

### Tier 2 — Reasonable but not urgent

**4. Oil futures (CL or MCL) — momentum or mean-reversion**
- Why: commodities ex-gold not covered. Oil has its own driver (geopolitics, inventory).
- Cost: similar to bond futures.
- Why not urgent: oil is volatile and noisy; harder to validate in 5 weeks than bonds.

**5. SPY/QQQ relative ratio**
- Why: tech vs broad-market rotation is real. But heavily correlated; edge is thin and transient.
- Why not urgent: most of the time it just tracks beta. Marginal edge over multi_orb on QQQ alone.

### Tier 3 — Skip (cost > benefit before freeze)

**Options strategies (vertical spreads, iron condors, premium selling)**
- Real gap, but options add 2-3x complexity (Greeks, IV term structure, expiration management). Can't validate in 5 weeks. Park for post-real-money phase.

**Direct international futures (NKD, DY)**
- International ETFs (tom_international with EEM/EWJ/VGK/EFA/FXI/INDA) already cover directional exposure. Marginal value over what we have.

**Crypto**
- Archived intentionally (Coinbase/Kraken paths in archive/). Different risk profile, different infra. Out of scope.

**1-minute / sub-minute strategies**
- Paper IBKR fills are unreliable at sub-minute scale. Even on real money, retail without co-location can't compete. Intentional skip.

## Timeframe coverage

| Timeframe | Coverage | Note |
|---|---|---|
| 1m / sub-minute | NONE | Intentional — paper unreliable, real-money retail untenable |
| 5m / 15m | argus FX, multi_orb, vix_intraday, spy_mean_rev | Solid |
| Hourly | gld_pm_long, fomc_drift | OK |
| Daily | gdx_gld, wick_gbpusd, jpy_pm_short, nq_overnight, aud_asian_breakout | Solid |
| 4-hour | tori | OK |
| Weekly+ | tom_international, rebalance, ares | Sparse but by-design |

**Verdict:** timeframe coverage is actually good. Not a real gap.

## Strategy-type coverage

| Type | Covered | Gap? |
|---|---|---|
| Trend following | titan, mamba, tori | OK |
| Mean reversion | spy_mean_rev, gdx_gld | OK (single-asset weak; gdx_gld is the pair) |
| Momentum / breakout | aud_asian_breakout, multi_orb, nq_overnight, nq_london_close | Solid |
| Event-driven | fomc_drift, apollo, tom_international, rebalance, themis | Solid |
| Long volatility | vix_intraday, vix_revert | Covered |
| Short volatility | NONE | **GAP — Tier 1 candidate above** |
| Pattern recognition | wick_gbpusd, cuebanks | OK |
| Pairs / relative-value | gdx_gld only | **GAP — Tier 1 candidate above** |
| Carry / interest-rate | NONE explicitly | **GAP — bond futures candidate addresses partly** |
| Options-based | NONE | Tier 3 skip |

## Decision: 2026-04-26 — ALL 3 APPROVED for build in Week 2

User greenlit all 3 candidates 4/26. Build window: 5/3 → 5/9 (Week 2 of the 5-week roadmap).

## CRITICAL — these 3 strategies are NOT equivalent in risk

The reviewer flagged this 4/26 and it must be respected:

| Strategy | Risk profile | Treatment |
|---|---|---|
| `zn_momentum` | **LOWEST** | Standard unproven-tier treatment. Mechanically cleanest of the 3. Main risks: contract roll / session logic / sizing / overnight. |
| `spy_tlt_pair` | **MEDIUM** | Standard unproven-tier with extra attention to leg matching. Risks: leg-mismatch fills, partial fills creating one-sided exposure, correlation regime shifts that violate mean-reversion assumption. |
| `vix_short` | **HIGHEST — special handling** | Short-vol products carry asymmetric tail risk. Cannot be treated as "just another unproven strategy." See below. |

### vix_short specific risk gates (mandatory)

Short-vol strategies look excellent until they meet convexity, gap risk, vol-of-vol, or instrument decay. The blowup is rarely in the signal — it's in what happens during a vol regime change. Specific guards required at build time:

- **Position cap halved**: max 0.5× anchor regardless of tier (override fleet_sizing default)
- **Hard stop ABOVE the spike high**, not below entry. The asymmetry kills you without it. Stop is the priority order, not the runner state.
- **Strategy-level daily loss limit**: -0.5% equity = pause that strategy for 24h (separate from fleet-wide -2% circuit)
- **No overnight exposure** for the first 60 days of operation. Close all positions by 15:55 ET regardless of state. Holding short-vol overnight is where blowups happen.
- **Stricter real-money exclusion** — vix_short stays paper-only for 90+ days after WINNER-CANDIDATE status. Even then, only after explicit second sign-off referencing the asymmetric risk profile.
- **Force-close on VIX > 30 print** — kill switch specific to this strategy. Vol-of-vol regime change voids the mean-reversion assumption; exit, don't fade.

These gates are NOT optional. They are the difference between "lost some paper money" and "wiped out the real account."

### Implementation spec — forge_zn_momentum

- **File:** `forge/zn_momentum/runner.py` (new)
- **Pattern:** port titan-style trend-follower logic. EMA crossover or breakout-of-N-day-range.
- **Instrument:** ZN (10-yr Treasury futures) primary; ZF (5-yr) fallback if ZN data unstable
- **IBKR_CLIENT_ID:** 118 (next free in forge range 100-199)
- **Timeframe:** daily bars, evaluate at NY close (16:00 ET)
- **Risk:** start at unproven tier (0.5% risk_pct)
- **Notional cap:** futures already at 5× anchor in fleet_sizing.json — fine
- **Build cost:** 4-6 hr (Mon-Tue 5/4-5/5)
- **First fires:** week of 5/11

### Implementation spec — forge_vix_short

- **File:** `forge/vix_short/runner.py` (new) OR add SHORT_VOL_MODE flag to existing vix_intraday and run as separate process with different IBKR_CLIENT_ID
- **Pattern:** sell-the-spike — when UVXY rallies > 5% intraday and shows reversal candles, short the bounce. Hard stop above the high.
- **Instrument:** UVXY (most liquid VIX-related ETF for retail)
- **IBKR_CLIENT_ID:** 119
- **Timeframe:** 5m or 15m
- **Risk:** start at unproven tier; SMALL — short-vol blowups are asymmetric. Position size capped at 0.5× anchor regardless of tier (override fleet_sizing default)
- **Hard stop ABOVE the spike high** — not optional, the asymmetry kills you without it
- **Build cost:** 2-4 hr (Wed 5/6)
- **First fires:** week of 5/11 if VIX cooperates

### Implementation spec — forge_spy_tlt_pair

- **File:** `forge/spy_tlt_pair/runner.py` (new) OR generalize gdx_gld_runner to take symbol pair as config
- **Pattern:** z-score on log(SPY/TLT) ratio. Enter when |z| > 2, exit on revert to mean OR stop at |z| > 3
- **Instrument:** SPY/TLT primary; SPY/IEF as alternative if TLT spread is too wide
- **IBKR_CLIENT_ID:** 120
- **Timeframe:** daily, evaluate at NY close
- **Risk:** unproven tier 0.5%. Net market exposure is theoretically near-zero (long one, short other) but reality is messier — size conservatively
- **Build cost:** 4-6 hr (Thu-Fri 5/7-5/8)
- **First fires:** when |z-score| > 2, which historically happens monthly-ish

### Total scope

- 12-16 hours of build distributed across Mon-Fri Week 2
- Each strategy: backtest first → walk-forward → signal-only → live IBKR paper
- All 3 verdict-eligible at 5/31 with adjusted thresholds (n ≥ 10 instead of n ≥ 30)
- IBKR client_ids 118, 119, 120 reserved — update `project_execution_conversion_20260424.md` table

### Anti-pattern guard

If by Friday 5/8 only 1 of 3 is live: cap at whatever's done. Don't extend the build window into Week 3 — Week 3 is risk-hardening, not strategy-building. The freeze still hits 5/31 regardless.

## Anti-patterns to avoid

- **"Add 5 things at once"** — cull effectiveness drops. Stick to 2-3 max.
- **"Build something novel"** — the user iterates verbally; novel strategies need long discovery cycles. These additions are PORTS of existing patterns, not new patterns.
- **"Skip the validation window"** — if added 5/9, the strategy has 3 weeks to prove fire rate + initial PF. If it doesn't, KILL on 5/31.
- **"Wait for post-5/31 freeze to add these"** — that's exactly what the freeze blocks. Add now or never.

## How to apply this memory

**Why:** strategy freeze is a hard line. Anything not in the fleet by 5/31 needs to wait until post-go-live. This is the last chance to fill genuine gaps before that lock takes effect.

**How to apply:**
- User decides which (if any) of the Tier 1 candidates to greenlight.
- Build effort goes in Week 2 of the 5-week roadmap.
- Each addition follows the same verdict template at 5/31 — no special treatment.
- If user says "yes to all 3" — verify cost + cull dilution risk before starting; might cap at 2.
- If user says "no, focus on culling" — that's also fine; document the deliberate gap so future-self knows it's deliberate, not oversight.
