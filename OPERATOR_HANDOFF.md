# Operator Handoff — 2026-05-23 evening → 2026-05-26 market open

Tuesday 5/26 is the next trading session (Monday 5/25 is Memorial Day, US
equity markets closed). This document lists every action that requires
operator (your) hands because they involve credentials, scheduled tasks,
process kills, or shared-state mutations.

Everything else from tonight's audit is already in the codebase. The fleet
is wired to behave correctly once you do these. Items are roughly ordered
by criticality.

---

## 0.0. TRUTH CORRECTION — xs_momentum CAGR was inflated (2026-05-25 evening)

**Every "18.04% CAGR" claim for forge_xs_momentum in prior docs / kill_log
entries / audit reports was wrong.** The actual 20y CAGR on broad_8 is
**9.96%** (and MaxDD 23.86%, MaxDD was reported close enough).

**Root cause** (fixed in this same commit batch):
`forge/xs_momentum/runner.backtest()` was grouping per-trade returns by
`exit_date` and compounding *only months with rotation*. Idle months —
when the held portfolio stayed the same — were silently dropped from
the compound but counted in the days-elapsed denominator for CAGR. Net
effect: ~80% inflation of the headline CAGR.

The bug was found by an independent backtest audit (see Agent 2 finding
in the 5/25 multi-agent debate). `forge.xs_momentum.runner.monthly_portfolio_returns()`
was already the honest calendar-walk version; the bug was that
`backtest()` had its own divergent path. Fix: extracted
`_calendar_monthly_returns` helper, called from both, with pinning
tests (`test_calendar_walk_includes_every_month_after_warmup`,
`test_backtest_cagr_matches_calendar_walk_compound`,
`test_monthly_returns_field_uses_calendar_walk`).

**Implication for the June 30 decision:** on the corrected number,
xs_momentum 1.0× **loses to a static risk-parity SPY/TLT/GLD portfolio**
on Sharpe (~0.70 vs ~0.78) and Sortino (~0.74 vs ~0.87). The CAGR
edge over 60/30/5/5 SPY/TLT/GLD/BIL is only ~3 pts and well inside
backtest noise. The honest framing flipped: **passive is the baseline;
Argus must overcome it, not vice versa.**

Historical docs that used 18.04%:
  - `OPERATOR_HANDOFF.md` lines 171, 228 (this file — left as historical artifact)
  - `ops/reports/system_audit/xs_momentum_*.md` (3 reports)
  - `argus_flow/configs/allocation_factors.json` _kill_log entries cite "18%/yr CAGR" implicitly
  - Memory files `project_2026_05_22_backtest_factory_findings.md`, `project_2026_05_23_pre_tuesday_hardening.md`

Don't bother retro-editing them — anyone reading those historicals will
land here via the truth-correction memory file.

---

## 0.5. New candidate: forge_nov_spy (2026-05-24 — needs your call)

Built and disciplined-gated tonight. **MARGINAL_PASS (8/9 layers)** —
the strongest gate result for any candidate this session.

20-year disciplined gate (n=30 Novembers, 1995-2025):
- PF point 4.92, IID CI lower **1.85 at 0bp, 1.80 at 5bp, 1.75 at 10bp**
  (well above 1.20 floor)
- Block bootstrap b=3,5,8 all PASS at 10bp
- p-value block bootstrap: **0.00030** (very strong)
- Period stability H1 (1995-2010, n=15): fail (CI lower 0.735 from
  small-sample noise)
- Period stability H2 (2010-2025, n=15): PASS **(PF 17.6, CI lower 5.30)**

Cross-era OOS:
- pre-2000: n=4 insufficient
- 2000-2010: PF 1.59, CI lower 0.39 — fails at slippage
- 2010-2026: **PF 18.66, CI lower 5.63** — modern era dramatically stronger

**The November effect has STRENGTHENED in modern data**, not arbitraged out.

Mechanics:
- Long SPY at close of first weekday of November
- Sell at close of last weekday of November
- One trade per year
- Holds for ~30 calendar days (~22 trading days)
- Max DD 8.68% (the smallest of any candidate this session)

Correlation with forge_tom_spy: moderate (overlap ~3 days in early
November; tom_spy is in-and-out, nov_spy holds the full month).

### How to activate

Same 4-step process as forge_tom_spy (see §0):
1. Add `forge_nov_spy` to `ACTIVE_ROSTER` in `test_sunset_roster.py`
2. Add `"forge_nov_spy": 0.2` to allocation_factors.json (recommend
   half of tom_spy's 0.3× since n=30 is borderline)
3. `python -m forge.nov_spy.runner --check` to confirm today's action
4. Launch `--loop` daemon

First action will be the first weekday of November 2026 (Mon Nov 2 —
about 5 months out, plenty of operator time to decide).

---

## 0. New candidate: forge_tom_spy (2026-05-24 — needs your call)

The new-strategy hunt produced its first candidate: classical turn-of-month
(TOM) effect on SPY. **Live runner is now built**; operator needs to
decide whether to (1) add to ACTIVE_ROSTER and (2) flip allocation_factor
above 0.0.

### Evidence stack

**20-year disciplined gate** (n=240):
- PF 1.90, IID CI lower 1.215 at 10bps slippage — passes 1.20 floor
- Block bootstrap (b=3,5,8): all pass
- Period-stability sub-samples fail individually (n=120 each) but point
  PFs are 1.717 / 1.721 — effect is consistent, just sample-limited
- **PARTIAL_PASS (6/9 layers)**

**Cross-decade OOS**:
- pre-2006: PF 1.59 / 2006-2016: PF 1.68 / 2016-2026: **PF 1.76**
- The "TOM was arbitraged out post-2000" literature claim is contradicted
  by SPY data

**Cohort risk audit** (`ops/reports/system_audit/cohort_risk_audit.md`):
- tom_spy ↔ xs_momentum correlation: **0.03** (essentially uncorrelated
  despite both trading SPY direction — different time-of-month exposure)
- Adding tom_spy at 0.3× to current portfolio: max DD 17.11% → **14.08%**
  (−18%), vol 10.78% → 9.11% (−15%), Sharpe 0.54 → 0.58
- Cost: 0.45%/yr CAGR reduction

### How to activate

1. Edit `argus_flow/tests/test_sunset_roster.py`: add `"forge_tom_spy"`
   to `ACTIVE_ROSTER`. The test_active_roster_is_exactly_xs_momentum_and_gld_pm_long
   pin will fail until you do this — by design, so re-activation requires
   a deliberate test edit.
2. Edit `argus_flow/configs/allocation_factors.json`: add
   `"forge_tom_spy": 0.3` to `factors` and a `_kill_log` line noting
   the recalibration source.
3. Run `python -m forge.tom_spy.runner --check` to confirm today's
   action (should print `is_tom_entry_day` and `is_tom_exit_day` flags).
4. Launch the daemon:
   ```powershell
   $env:IBKR_PORT = '7497'
   Start-Process -FilePath 'C:\Argus\.venv\Scripts\python.exe' `
     -ArgumentList '-m','forge.tom_spy.runner','--loop' `
     -WorkingDirectory 'C:\Argus\repo' -WindowStyle Hidden
   ```
5. The runner is calendar-aware; first action will be the next entry
   day (likely the 4th-to-last weekday of either May or June 2026 —
   `--check` will tell you).

### Momentum crash filter on xs_momentum — REJECTED (the filter hurts)

Tested whether adding a Daniel-Moskowitz "Momentum Crashes" regime
filter (skip rebalance when SPY 6mo return < 0 AND realized vol in top
25%) would reduce xs_momentum's max DD.

**Counter-intuitive result: the filter HURTS, not helps.**

  Metric        Unfiltered    Filtered    Delta
  PF            3.79          2.97        -0.82
  Win rate      62.5%         57.5%       -5.0pts
  Max DD        22.39%        36.36%      +13.97pts WORSE
  Excluded:     0             8 trades
  avg pnl of excluded:                    +9.60% (winners!)

WHY THE FILTER FAILS

The classical Daniel-Moskowitz finding is for single-asset-class
momentum (US stock cross-sectional). For xs_momentum's broad-8
universe (SPY/QQQ/IWM/DIA/EFA/EEM/**GLD/TLT**), the cross-sectional
rank logic NATURALLY rotates INTO safe-haven assets (GLD, TLT)
during bear + high-vol regimes — and those rotations are where the
biggest upside concentration happens. The universe IS the regime
filter.

A regime filter that goes to cash during these conditions throws
away the strategy's adaptive defensive behavior. The excluded trades
in this backtest averaged +9.60% — they were exactly the winning
defensive rotations into GLD/TLT during March 2020, late 2022, etc.

NO ACTION. xs_momentum stays without the regime filter. The current
DD profile is the strategy's natural floor at this universe.

Audit re-runnable: `python -m ops.audit.run_xs_momentum_crash_filter`.

---

### Dual-momentum trend-following on broad-8 — FAIL with nuance

Tested per-asset absolute-momentum trend-following (Antonacci-style)
on the same broad-8 universe xs_momentum uses. For each asset, each
month: hold if trailing 12-month return > T-bill threshold; else flat.

20y backtest, 1146 asset-month trades:
  PF 1.53, WR 59.7%, CAGR 7.27%, max DD 26.41%
  Fraction-of-time-invested per asset: 63%

Disciplined gate: **HEADLINE FAIL (3/8 layers)** — but with structure
that matters:
  IID @ 0bp:   PASS (CI lower 1.31)
  IID @ 10bp:  PASS (CI lower 1.24)
  Block boot b=3,5,8: ALL FAIL (CI lower 1.15-1.18, just below 1.20 floor)
  Period stability H1 (2006-2016): fail (CI lower 1.04)
  Period stability H2 (2016-2026): **PASS (CI lower 1.33)**
  p-value: 0.047 (marginal)

Cross-era OOS shows modern-era strengthening:
  2006-2016:  n=638  PF=1.42  CI lower=1.15  (fails)
  2016-2026:  n=622  PF=1.60  **CI lower=1.30 (passes)**

Same pattern as nov_spy and xs_momentum: modern era stronger than
historical. But the absolute level of edge (CAGR 7.27%, DD 26.41%)
is NOT competitive with xs_momentum (CAGR 18.04%, DD 22.39%).

NO ACTION. dual_trend is not added as a candidate. The cross-sectional
RANK approach of xs_momentum (top-2 of 8) is materially better than
the absolute MOMENTUM approach (hold-if-above-threshold) on this same
universe.

Code stays in forge/dual_trend/ as research artifact + reusable
template. Audit re-runnable: `python -m ops.audit.run_dual_trend_evaluation`.

---

### TOM on IWM — dead (opposite of SPY where TOM is alive)

Re-ran the TOM (4/3) backtest on IWM (Russell 2000 small-cap) to test
whether small-cap liquidity constraints preserve TOM edge that broad-
market arbitrage might have cleaned up. Result is the OPPOSITE of
naive intuition: TOM is alive on SPY but DEAD on IWM.

  Ticker  PF    CI lower @ 10bp  Layers   Verdict
  SPY     1.90  1.215            6/9      PARTIAL_PASS
  IWM     1.50  0.994            0/9      FAIL

Cross-decade OOS on IWM shows steady decay:
  pre-2006: PF 1.89 (alive)
  2006-2016: PF 1.49 (weakening)
  2016-2026: PF 1.31 (almost dead, CI lower 0.81)

The classical literature claim "TOM stronger on small-caps" is NOT
supported in modern data. Likely mechanism: IWM (started 2000)
democratized small-cap access right at the peak of algorithmic
arbitrage. Pre-IWM, small-cap access had high friction; with IWM,
algos fully arbitraged the calendar effect.

NO ACTION

IWM TOM is not a viable candidate. forge_tom_spy stays SPY-only.

Combined calendar-anomaly picture from this session:
- TOM SPY: PARTIAL_PASS, ready to ship (operator opt-in pending)
- TOM IWM: FAIL
- November SPY: MARGINAL_PASS, ready (operator opt-in pending)
- FOMC drift SPY: FAIL (dead since ~2018)
- Russell recon IWM: null
- DOW SPY: dead post-2000
- DOW IWM: dead

Audit re-runnable: `python -m ops.audit.run_tom_spy_evaluation --ticker IWM`.

---

### xs_momentum universe comparison — sector_only matches broad_8

Tested whether the current xs_momentum universe (broad_8 = SPY/QQQ/IWM/
DIA/EFA/EEM/GLD/TLT) is actually optimal vs alternative universes:

  Universe                       PF    CI_lower@10bp  CAGR    Max DD   Layers
  broad_8 (current)              3.79      1.556      18.04%  22.39%   4/8 PP
  sector_only (10 US sectors)    3.78      1.604      21.87%  25.76%   5/8 PP
  country_only (8 markets)       1.37      0.667       3.48%  51.58%   0/8 FAIL

KEY FINDING

sector_only PARTIAL_PASSES with **higher CI lower and CAGR** than the
current broad_8 baseline — but with worse max DD (25.76% vs 22.39%).
Different risk profile, not a clear winner. Sector-only passes ALL 3
block_bootstrap layers; broad_8 fails 2 of 3.

The 5/22 finding that "sector-only fails the disciplined gate (CI lower
1.05 at 10bps)" was at DIFFERENT params (probably top-3 picks or
different lookback). At current PARAMS (252/21 lookback, top-quintile
fraction = 0.2 → 2 picks from broad_8 / 2 picks from sector_only), the
sector universe is in the same PARTIAL_PASS band.

Country-only is decisively worse (PF 1.37 = breakeven). Cross-country
dispersion alone is NOT the driver of xs_momentum's edge.

NO ACTION

Both broad_8 and sector_only PARTIAL_PASS. broad_8 has slightly better
risk-adjusted profile (lower DD for similar PF). Production strategy
stays on broad_8. The audit is informational; no allocation change.

If operator wants to experiment in a future session, sector_only could
be a 2nd cross-sectional-momentum slot (different DD timing, possibly
diversifying with broad_8).

Audit re-runnable: `python -m ops.audit.run_xs_momentum_universe_comparison`.

---

### Day-of-week effects on IWM — also DEAD (extended null)

Re-ran the DOW audit on IWM (Russell 2000 small-cap, 2000-2025,
n=6,436 daily bars) to test whether small-cap liquidity constraints
preserved any residual edge that SPY arbitraged out.

Bonferroni-adjusted at 5 tests (α=0.01):

  Day  n     mean_bps  t-stat  p       Bonf-sig?
  Mon  1208  +1.51     +0.33   0.7430  no
  Tue  1321  +9.07     +2.21   0.0268  no (above Bonferroni)
  Wed  1320  +4.33     +1.04   0.3004  no
  Thu  1295  +1.35     +0.32   0.7531  no
  Fri  1292  +4.90     +1.27   0.2028  no

Tuesday is suggestive (highest mean, p=0.027) but does NOT survive
multi-testing correction. The Friday-effect strategy (buy IWM Thu
close, sell Fri close): PF=1.03, CI=[0.88, 1.19] — solidly inside
the no-edge band, same as SPY.

Conditional weekend effect (Mon after negative Fri): mean +0.46 bps,
t=+0.06, p=0.95 — null.

**Class of DOW-on-equity strategies is now definitively closed across
both large-cap (SPY) and small-cap (IWM).** Arbitrage extends to
small-caps despite their lower liquidity.

Audit re-runnable: `python -m ops.audit.run_dow_research --ticker IWM`.

### Day-of-week effects — DEAD post-2000

Tested 7,800 SPY daily bars (1995-2025) for systematic Mon-Fri patterns.
Bonferroni-adjusted at 5 tests (α=0.01):

  Era         Day  mean_bps   t     p       Bonf-sig?
  Full        Tue  +7.72     2.60  0.0094   YES
  Pre-2000    Fri  +18.24    2.79  0.0052   YES  ← real then
  Post-2000   —    nothing significant

Friday-effect strategy (buy Thu close, sell Fri close): **PF=1.04,
CI=[0.90, 1.20] post-slippage — no edge**. The pre-2000 Friday effect
has been arbitraged out; the full-window Tuesday signal is driven by
pre-2000 leakage.

Conditional weekend effect (Monday after negative Friday is supposedly
weak): tested at n=627, mean +6.47 bps, t=+1.06, p=0.29 — also dead.

ACTION: nothing actionable. Day-of-week strategies on SPY do not have
edge. Class of strategies closed.

Audit re-runnable: `python -m ops.audit.run_dow_research`.

### Monthly seasonality / Halloween effect — November is the only significant month

Tested 30 years of SPY monthly returns (1995-2025, n=371). Bonferroni-
adjusted at 12 calendar months (α = 0.00417):

- **November**: mean +2.77%/month, t=+3.75, p=0.0002, WR 80.6% — only
  Bonferroni-significant month
- April (mean +1.90%, t=+2.34) and July (+1.46%, t=+2.08) are
  individually p<0.05 but don't survive multi-testing correction
- Aug & Sep have NEGATIVE mean returns (−0.26%, −0.40%) but not
  significant

Classical "Halloween indicator" (winter vs summer): +0.60%/month winter
outperformance, but p=0.18 — NOT statistically significant. Most of
the seasonal effect is concentrated in November alone.

**Possible candidate**: `forge_nov_spy` — long SPY for the month of
November. Single trade per year, n=30 historical observations
(borderline for disciplined gate). Has natural overlap with tom_spy's
late-Oct + early-Nov window (~7 days shared), so the strategies would
be moderately correlated. Worth prototyping in a follow-up batch if
you want a calendar-anomaly counterpart to tom_spy.

Audit re-runnable: `python -m ops.audit.run_seasonality_research`.

### forge_fomc_drift — STAYS ARCHIVED (resurrection denied)

forge_fomc_drift was killed 2026-05-20 for zero live fires (operational).
Re-ran the existing 2000-2026 backtest (n=216) through the disciplined
gate to see if it should be resurrected. **It should not.**

  Era                 n   PF    WR     avg/trade   pass @ 5bp?
  2000-2010 (pre-QE) 85   1.80  62.4%  +0.33%      no (CI lower 1.00)
  2010-2018 (QE era) 64   1.53  48.4%  +0.15%      no (CI lower 0.73)
  2018-2026 post-QE  67   0.93  40.3%  −0.03%      no (CI lower 0.49)

The edge was real pre-2010 (PF 1.80) but has been DEAD since ~2018
(point PF 0.93, WR 40%, avg −0.03% per trade — net loser at any
slippage). Fed forward guidance + dot plots after 2010 killed the
pre-announcement uncertainty premium that drove the drift.

Disciplined gate: FAIL 0/9 layers. The legacy "PF 1.58" headline was
averaging strong pre-QE returns into negative-expectancy post-QE
returns and misleading.

ACTION: nothing. forge_fomc_drift stays archived at allocation 0.0×.

Audit re-runnable: `python -m ops.audit.run_fomc_drift_evaluation`.

### Russell reconstitution candidate — NULL RESULT

Also tested IWM around the annual late-June Russell rebalance Friday.
Across 16 years (2010-2025), no window reaches conventional significance:
- pre5_to_friday: mean +1.03%, t=+1.45 (best targeted window, ns)
- full_20day: mean +1.82%, t=+1.88 (most cumulative)

The Russell recon edge requires targeted add/delete trades (specific
predicted stocks), not a broad IWM bet. That's a much bigger data
project. Report at `ops/reports/system_audit/russell_recon_research.md`.

---

## 1. Rotate the Discord webhook (security — do this BEFORE pushing)

The webhook URL committed in `.env` line 96 (DISCORD_WEBHOOK_URL)
is public to anyone who has ever cloned the repo. It's still in git
history. The fix is rotation, not relocation.

Steps:

1. In Discord: Server Settings → Integrations → Webhooks → find the
   one pointing at the channel Argus uses → click the existing webhook →
   click **Reset URL**. Copy the new URL.
2. Create `secrets/discord.env` (`secrets/` is already in `.gitignore`)
   with the new URL:
   ```
   DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/<NEW-ID>/<NEW-TOKEN>
   ```
3. Remove (or comment out) line 96 of `.env`. The notify.py loader I
   shipped tonight already reads `secrets/discord.env` before falling
   back to `.env`, so nothing else needs changing.
4. (Optional) Run `python -m ops.notify --test "rotated webhook"` to
   confirm the new URL works.

If you skip rotation, the OLD URL stays live in git history forever and
anyone with read access can post to your Discord.

---

## 1.47. Tuesday 2026-05-26 evening — Gateway API issue (NEEDS MANUAL CLICK)

**Symptom**: PM-window runners (`forge_gld_pm_long`, `forge_uso_pm_long`) silently failed every signal-hour evaluation since the 5/22 reset with `BrokerEquityUnavailableError: broker equity unavailable and last-known-good cache absent`. Vetting rollup (`ops/audit/run_vetting_rollup.py`) surfaced it as a cadence violation: expected ~5 PM-window fills since reset, got 0.

**Root cause chain**:
1. Gateway TCP port 4002 was up (Java process listening) but the IBKR API socket was refusing connections.
2. Cascade: runner connects to IBKR fine -> calls `get_sizing_anchor_usd()` -> reads `risk_oversight_report.json` -> sees `account_equity_usd: 0.0` (because `argus_flow.ops.refresh_broker_equity` had been silently failing for hours with `ConnectionRefusedError`) -> raises `BrokerEquityUnavailableError`.
3. This is *correct architectural behavior* per CLAUDE.md rule #2 (no fallback on broker equity) -- but the failure surface was invisible because no alert fired.

**Partial fix shipped 2026-05-26 19:42 UTC**:
- Killed stale Java (PID 22896 from earlier session).
- Patched `C:\IBC\config_gateway.ini` line 329: `ExistingSessionDetectedAction=primary` -> `primaryoverride`. Without this, the new Gateway instance saw "Existing session detected (scenario 4)" and shut itself down rather than taking over.
- Triggered `ArgusGatewayWatchdog` scheduled task. New Java (PID 7448) came up, IBC drove through login + Warning + Login Messages dialogs successfully.

**Still broken**: Gateway API socket on 4002 still refuses connections. Most likely Gateway has its "Enable ActiveX and Socket Clients" disabled or trusted-IPs missing. Because `C:\Jts\jts.ini` has `useRemoteSettings=true`, the API config is loaded from IBKR's server-side account settings.

**Operator action required** (pick ONE path):
1. **Manual GUI path**: Right-click the Gateway icon in system tray -> Restore -> Configure -> API -> Settings -> verify "Enable ActiveX and Socket Clients" is checked, Socket port is 4002, "Read-Only API" is unchecked, and 127.0.0.1 is in Trusted IPs. OK -> let runners retry on next signal hour.
2. **IBKR web path**: Log into Account Management at interactivebrokers.com -> Settings -> API -> enable the same flags. Restart Gateway via watchdog task.

**Verification command** (run after the fix):
```powershell
C:\Argus\.venv\Scripts\python.exe -m argus_flow.ops.refresh_broker_equity
# Then check: risk_oversight_report.json should show account_equity_usd > 0.
# PM runners will pick it up automatically on their next signal-hour wake (18/19/20 UTC).
```

**Why I left it unfixed**: API enable/trusted-IP settings live behind a GUI checkbox or in IBKR Account Management, neither of which I can drive programmatically without UI automation. Documented here so any future Claude session sees the trail.

---

## 1.46. Tuesday 2026-05-26 afternoon batch — STATUS

**Compatibility-matrix sweep + 4-strategy expansion (v26 → v29).** Built and ran `ops/audit/run_extension_matrix_sweep.py` testing TOM + 12-month single-month patterns × 33 ETFs at 6bps RT slippage. 26 SURVIVES / 5 MARGINAL / 266 FAIL out of 297 cells. Plus earlier-in-day breakout sweep (EWZ won) + PM-pattern sweep (USO won). Net additions to active roster:

- `forge_uso_pm_long` (v27, client_id 131, alloc 0.3×) — extends gld_pm_long to USO. Hourly PM-window intraday.
- `forge_ewz_breakout` (v28, client_id 132, alloc 0.2×) — 21-day high breakout on EWZ. Daily, ~3-4 fills/yr.
- `forge_ief_jul_hold` (v29, client_id 133, alloc 0.1×) — Treasury July seasonal. One trade/yr (1st→last weekday of July).
- `forge_gld_jan_hold` (v29, client_id 134, alloc 0.1×) — gold January seasonal. One trade/yr.

The IEF + GLD picks are deliberately the only matrix survivors uncorrelated with the existing US-equity-beta cohort. The other 22 matrix survivors are tom_spy/nov_spy clones on correlated tickers — left as documented intelligence for the post-vetting review next month.

**Fleet now stands at 16 active runners summing 4.50× anchor.** Vetting phase begins: no new strategy adds until end-of-June review unless a bug forces it. Weekends-only for tweaks. See `ops/reports/system_audit/extension_matrix_sweep.md` for the full matrix.

---

## 1.45. Tuesday 2026-05-26 morning recovery — STATUS

**Executed by Claude at ~07:57 ET on 2026-05-26** (commit pending). What was done:

| Gate | Item | Status | Note |
|---|---|---|---|
| 1 | Disable 7 stale `ArgusXxxLoop` scheduled tasks | **OPERATOR TODO (admin)** | `Disable-ScheduledTask` denied at user-level. Must run in elevated PowerShell. |
| 2 | Stop 8 zombie KILLED-strategy runner processes | DONE | All zombies stopped; verified 0 remaining. |
| 3 | Kill stuck TWS (PID 20448, "Attempt 41 auth") | DONE | Only Gateway listening on 4002. |
| 4 | Launch v26 fleet via `start_post_reset_runners.ps1` | DONE | 16 runners launched (added forge_uso_pm_long client_id=131, forge_ewz_breakout 132, forge_ief_jul_hold 133, forge_gld_jan_hold 134 across 2026-05-26 sessions). `tom_spy`/`nov_spy`/`ief_jul_hold`/`gld_jan_hold` wake daily ~19:40-19:50 UTC. |
| 5 | Register persistent scheduled tasks | DONE | `ArgusReplayHealth` (daily 10:30pm), `ArgusDailyHealth` (daily 11:00pm), `ArgusV26FleetStartup` (every 2h — `AtLogOn` trigger needed admin so used 2h-interval as compromise). |
| 6 | Verify fleet | DONE | All 16 runners alive, heartbeats <2min old. `canonical_fills.jsonl` will populate when intraday strategies fire (gld_pm_long, uso_pm_long PM windows). |
| 7 | Broker setup | PARTIAL | Created `C:\IBC\config_gateway.ini` with Gateway path (`IbDir=C:\Jts\ibgateway\1037`) + port (`OverrideTwsApiPort=4002`). Registered `ArgusGatewayWatchdog` (every 2h, self-heal — exits clean if Gateway already up). **OPERATOR TODO**: fill `IbLoginId` (line 83) + `IbPassword` (line 88) with real paper-account credentials. Until then, `start_gateway_via_ibc.ps1` will refuse with `exit 3: PLACEHOLDER values`. |

### Operator-only items remaining (need elevated PowerShell + admin)

```powershell
# Run as Administrator (Win+X → "Windows PowerShell (Admin)")

# A. Disable 7 stale loop tasks that auto-restart KILLED strategies
$stale = 'ArgusMultiOrbLoop','ArgusVixIntradayLoop','ArgusSpyMeanRevLoop',
         'ArgusCueBanksPaperLoop','ArgusAudOrbLoop','ArgusNqLondonCloseLoop',
         'ArgusToriPaperLoop'
foreach ($t in $stale) { Disable-ScheduledTask -TaskName $t }

# B. Replace ArgusV26FleetStartup (currently every-2h) with at-logon + every-4h
#    for faster reboot recovery.
Unregister-ScheduledTask -TaskName 'ArgusV26FleetStartup' -Confirm:$false
$a = New-ScheduledTaskAction -Execute 'PowerShell.exe' `
       -Argument '-NoProfile -ExecutionPolicy Bypass -File "C:\Argus\repo\ops\start_post_reset_runners.ps1"'
$t1 = New-ScheduledTaskTrigger -AtLogOn
$t2 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddHours(4) `
        -RepetitionInterval (New-TimeSpan -Hours 4)
Register-ScheduledTask -TaskName 'ArgusV26FleetStartup' -Action $a `
  -Trigger @($t1,$t2) -Description 'Launch v26 active fleet'
```

### Operator-only items requiring you (creds + decisions)

1. **Fill IBC Gateway credentials** in `C:\IBC\config_gateway.ini`:
   - Line 83: `IbLoginId=PLACEHOLDER_YOUR_PAPER_USERNAME` → real paper username
   - Line 88: `IbPassword=PLACEHOLDER_YOUR_PAPER_PASSWORD` → real paper password
   After this, the `ArgusGatewayWatchdog` task can actually launch Gateway. Until you do, Gateway must be launched manually after every reboot.

2. **Discord webhook rotation** still pending (URL in tracked `.env` line 96 — exposed in git history).

3. **The remaining `OPERATOR_HANDOFF.md §1.5` is the canonical reference if you need to repeat this recovery later.**

---

## 1.5. Power-cycle / morning recovery (new — 2026-05-25 evening)

After a power outage or Windows reboot, the fleet ends up in a known-bad
split-brain state because the surviving scheduled tasks were registered
for pre-sunset strategies. Symptoms verified 2026-05-25 evening:

- 8 zombie KILLED-strategy runner processes restarted automatically:
  - `forge.multi_orb.runner --loop` (×2 PIDs)
  - `forge.vix_intraday.runner --loop` (×2)
  - `forge.spy_mean_rev.runner --loop` (×2)
  - `forge.cuebanks.paper_bridge --loop` (×2)
- 0 active-roster runners came up (xs_momentum, tom_spy, nov_spy,
  tail_hedge, xs_momentum variants, gld_pm_long — nothing).
- Both **TWS** and **IB Gateway** running simultaneously:
  - TWS PID 4384 stuck on "Attempt 5: Authenticating..." (failed-login
    retry loop, port 7497 — pre-migration default).
  - IB Gateway PID 7140 healthy on port 4002 — this is the post-5/25
    canonical broker.

The KILLED registry blocks `submit_bracket` from accepting orders from
zombie runners, so they cannot trade. But they're consuming client_id
slots, generating log noise, and burning CPU. And nothing is actually
trading the live allocation.

### Recovery sequence (run in PowerShell — verify at every gate before proceeding)

**Order matters.** Per the 5/25 multi-agent debate, neutralize stale
state BEFORE bringing up the new fleet, so killed-strategy zombies
can't pollute canonical_fills alongside legitimate runners.

```powershell
# ─── Gate 1: Stop scheduled tasks that auto-restart KILLED strategies ───
$staleTasks = 'ArgusMultiOrbLoop','ArgusVixIntradayLoop','ArgusSpyMeanRevLoop','ArgusCueBanksPaperLoop','ArgusAudOrbLoop','ArgusNqLondonCloseLoop','ArgusToriPaperLoop'
foreach ($t in $staleTasks) { Disable-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue | Out-Null; Write-Host "Disabled task: $t" }
# Verify: re-list to confirm State=Disabled
Get-ScheduledTask | Where-Object { $_.TaskName -in $staleTasks } | Select-Object TaskName, State | Format-Table -AutoSize

# ─── Gate 2: Stop all KILLED-strategy runner zombies ────────────────────
$killedPattern = 'forge\.(multi_orb|vix_intraday|spy_mean_rev|cuebanks|tori|aud_asian_breakout|wick_gbpusd|jpy_pm_short|mamba|nq_london_close|nq_overnight|pead|spy_trend_follower|fomc_drift|tom_international|vix_revert|vix_carry|coint_pairs|gdx_gld|rebalance|atlas|themis)\.'
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match $killedPattern } |
  ForEach-Object { Write-Host "Stopping PID $($_.ProcessId): $($_.CommandLine.Substring(0,[Math]::Min(100,$_.CommandLine.Length)))"; Stop-Process -Id $_.ProcessId -Force }
# Verify: re-run the same selector — should be empty now
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match $killedPattern } | Measure-Object | Select-Object Count

# ─── Gate 3: Resolve dual-broker conflict ───────────────────────────────
# Pick exactly ONE path before continuing.
#
# Path A — kill TWS, keep IB Gateway as sole broker:
$tws = Get-Process -Name 'tws' -ErrorAction SilentlyContinue
if ($tws) { Stop-Process -Id $tws.Id -Force; Write-Host "Killed TWS PID $($tws.Id)" }
# Path B — fill IBC credentials and use TWS via IBC:
#   notepad C:\IBC\config.ini   # set IbLoginId + IbPassword on lines 83/88
#   .\ops\start_tws_via_ibc.ps1
#   then kill Gateway: Get-Process -Name 'ibgateway' | Stop-Process -Force
#
# Verify: exactly ONE listener on port 4002 OR 7497, not both
Get-NetTCPConnection -State Listen | Where-Object { $_.LocalPort -in 4002,7497 } | Select-Object LocalPort, OwningProcess, @{N='Process';E={(Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).ProcessName}} | Format-Table -AutoSize

# ─── Gate 4: Launch the v26 active fleet (Gateway 4002) ─────────────────
.\ops\start_post_reset_runners.ps1 -DryRun     # show what will fire
.\ops\start_post_reset_runners.ps1             # for real
# Verify: launcher exits 0, all 16 runners listed as launched/already-running

# ─── Gate 5: Register persistent scheduled tasks (so this isn't manual next time) ───
# (One-time setup; skip if already registered.)
$python = 'C:\Argus\.venv\Scripts\python.exe'
$repo = 'C:\Argus\repo'
# V26 fleet auto-startup (at logon + every 4h thereafter):
$action = New-ScheduledTaskAction -Execute 'PowerShell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$repo\ops\start_post_reset_runners.ps1`""
$trigger1 = New-ScheduledTaskTrigger -AtLogOn
$trigger2 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddHours(4) -RepetitionInterval (New-TimeSpan -Hours 4)
Register-ScheduledTask -TaskName 'ArgusV26FleetStartup' -Action $action -Trigger @($trigger1,$trigger2) -Description 'Launch v26 active fleet (replaces ArgusFleetStartup)' -Force
# Replay-health nightly cron (so the pre-trade bridge actually activates after day 35):
$rhAction = New-ScheduledTaskAction -Execute $python -Argument "-X utf8 -m ops.audit.run_replay_health_check" -WorkingDirectory $repo
$rhTrigger = New-ScheduledTaskTrigger -Daily -At 22:30
Register-ScheduledTask -TaskName 'ArgusReplayHealth' -Action $rhAction -Trigger $rhTrigger -Description 'Nightly replay-vs-ledger health check' -Force
# Daily-health (composes auto_pause + orphan + preflight + flag_check):
$dhAction = New-ScheduledTaskAction -Execute $python -Argument "-X utf8 -m ops.daily_health_check" -WorkingDirectory $repo
$dhTrigger = New-ScheduledTaskTrigger -Daily -At 23:00
Register-ScheduledTask -TaskName 'ArgusDailyHealth' -Action $dhAction -Trigger $dhTrigger -Description 'Daily aggregated safety/audit health check' -Force

# ─── Gate 6: Verify canonical artifacts are receiving fresh rows ────────
# Wait ~30 min for the first runner heartbeat / first signal evaluation, then:
$env:PYTHONIOENCODING = 'utf-8'
& $python -X utf8 -m ops.audit.run_fleet_snapshot --skip-yfinance --json |
  ConvertFrom-Json | Select-Object -ExpandProperty strategies |
  Where-Object { $_.allocation -gt 0 } |
  Format-Table strategy, status, allocation, heartbeat_age_h -AutoSize
# Also check canonical_fills is being written to. It will likely stay
# 0 bytes until an intraday strategy actually fires (gld_pm_long should
# write within ~2h of market open; xs_momentum variants wait for month-end).
Get-Item argus_flow\logs\canonical_fills.jsonl | Select-Object Length, LastWriteTime
```

### Why these scheduled tasks are stale

The `ArgusMultiOrbLoop` / `ArgusVixIntradayLoop` / etc. tasks were
registered before the 5/20 sunset batch. They predate the kill registry
formalization (5/24) and the post-reset 5-survivor roster (5/22). The
right long-term fix is to re-register a single `ArgusV26FleetStartup`
task that runs `start_post_reset_runners.ps1` at logon — but disabling
the stale ones first prevents the zombie restart cycle.

### Why TWS is up at all

The 5/25 Gateway-migration commit (4f6cb0c) cut over the runners but
did NOT remove TWS auto-start. TWS launches via `C:\Jts\tws.exe` —
either from a Startup shortcut, a desktop shortcut the operator
clicked, or via the IBC scheduled task. Until `C:\IBC\config.ini`
credentials are filled in OR the TWS startup is fully disabled, TWS
will continue launching and locking out the paper account login.

---

## 2. Start TWS / verify IBKR session is up

`nq_overnight` was stuck overnight in stale signal-only state because
TWS had been unreachable during the 5/22 reset window. I shipped a
self-heal that clears that stale state on the next wake **provided TWS
is up**, but if TWS itself is down on Tuesday morning, no strategy
trades.

Operator items:

- Confirm TWS is running, paper account DUP472829, port **7497**.
- IBC was installed 5/22 but the credentials in `C:\IBC\config.ini` are
  still PLACEHOLDER (`IbLoginId=PLACEHOLDER`,
  `IbPassword=PLACEHOLDER`). Either:
  - **Manual launch path**: log in to TWS yourself before 14:30 UTC
    Tuesday and leave it running. Acceptable until you fill IBC creds.
  - **Auto-launch path**: edit `C:\IBC\config.ini`, fill in the real
    paper credentials, then run `ops/start_tws_via_ibc.ps1` to verify
    IBC can log in unattended. Optionally register the
    `TWSAutoStart` scheduled task per `ops/IBC_SETUP.md`.

---

## 2.15. Kill the still-running KILLED-strategy processes (new — 2026-05-24)

Fleet snapshot caught that 4 KILLED strategies still have live runners
(< 5h heartbeat age):
- forge_gdx_gld (0.01h)
- forge_nq_overnight (1.0h)
- forge_spy_trend_follower (1.5h)
- forge_pead (4.4h)

Plus 12 more with stale heartbeats from ~5/22 23:00 UTC (~43h ago)
that haven't shut down cleanly:
  forge_atlas / aud_asian_breakout / cuebanks / fomc_drift /
  jpy_pm_short / mamba / nq_london_close / rebalance / themis /
  tom_international / tori / vix_revert / wick_gbpusd

These can't trade (allocation=0 + KILLED_STRATEGY_CUTOFFS blocks
submit_bracket at the runtime invariant) but they're:
- Consuming TWS client_id slots (IBKR per-account limit applies)
- Producing log noise that masks real signals
- Holding heartbeat files that make capacity_stress include them
  as "current positions" if they have stale state.json

Run this in **PowerShell** to find and stop them:

```powershell
# List killed-strategy python processes
Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq 'python.exe' -and (
        $_.CommandLine -match 'forge\.(gdx_gld|nq_overnight|spy_trend_follower|pead|atlas|aud_asian_breakout|cuebanks|fomc_drift|jpy_pm_short|mamba|nq_london_close|rebalance|themis|tom_international|tori|vix_revert|wick_gbpusd|spy_mean_rev|multi_orb|vix_intraday|vix_carry|coint_pairs)\.runner'
    )
} | Select-Object ProcessId, CommandLine | Format-List

# Then kill each by PID (review before running):
# Stop-Process -Id <pid> -Force
```

After stopping, re-run `python -m ops.audit.run_fleet_snapshot` to
confirm only ACTIVE + PENDING_OPT_IN runners are still heartbeating.

---

## 2.18. Fleet snapshot single-command report (new — 2026-05-24)

`python -m ops.audit.run_fleet_snapshot` produces a comprehensive
single-shot report combining:
- Roster classification (calls run_roster_state)
- allocation_factors snapshot + recent kill_log
- capacity_stress per-strategy max_safe_multiplier
- heartbeat age per runner
- canonical_fills counts since the current epoch
- xs_momentum current top-2 picks (live yfinance query)
- preflight verdict per ACTIVE strategy

Output:
- stdout: human-readable markdown
- `ops/reports/system_audit/fleet_snapshot.md`: persisted MD
- `ops/reports/system_audit/fleet_snapshot.json`: machine-readable

Use this as the FIRST thing to read when sitting down to audit —
captures the live state in one place.

Flags: `--json` for JSON output, `--skip-yfinance` for offline mode.

---

## 2.2. Roster is now formally classified — 2 ACTIVE / 28 KILLED (new — 2026-05-24)

Per your direction ("make sure the poor performers are killed, archive
in a month or so if no revival"), every strategy that was at
allocation=0.0 but NOT in the formal kill registry has been moved into
`KILLED_STRATEGY_CUTOFFS`. This closes a real safety gap: previously
those LIMBO strategies could have been started by an accidental runner
launch and the runtime invariant wouldn't have refused entries.

CURRENT STATE (run `python -m ops.audit.run_roster_state`):

  ACTIVE:           forge_xs_momentum (1.0×) + forge_gld_pm_long (0.5×)
  PENDING_OPT_IN:   forge_tom_spy (built, awaiting your call)
                    forge_nov_spy (built, awaiting your call)
  KILLED:           28 strategies (every other entry in
                    allocation_factors.json, plus the 3 argus FX pairs +
                    apollo + hermes + titan Greek scanners)
  LIMBO:            0
  ABANDONED:        0

The kill is enforced at 3 layers:
  1. allocation_factor = 0.0 (capital layer)
  2. helio.roi_filter.KILLED_STRATEGY_CUTOFFS (runtime invariant —
     submit_bracket refuses entries from these strategies)
  3. helio.fleet_monitor.SYSTEMS no_restart=True (process layer — fleet
     monitor doesn't auto-restart a dead runner)

REVIVAL PROCESS (when you want to bring one back in a month)

Each killed strategy is removed by:
  1. Edit helio/roi_filter.KILLED_STRATEGY_CUTOFFS — remove the entry
  2. Add a kill-log line in allocation_factors.json explaining the
     revival rationale + the disciplined-gate evidence supporting it
  3. Flip allocation_factor > 0
  4. If runner exists, restart it

The kill registry test enforces these layers stay aligned (so a partial
revert can't accidentally resurrect a killed strategy).

NEW AUDIT SCRIPT

`python -m ops.audit.run_roster_state` runs anytime + reports
classification per strategy + flags LIMBO / ABANDONED entries.
Returns exit code 0 if clean, 1 if drift is detected. Suitable for
the daily health check chain if you want it added.

---

## 2.25. epoch_reset now clears killed-strategy phantoms (new — 2026-05-24)

Discovered tonight that the 5/22 reset DID NOT clear `state.open_trade`
on killed strategies — that's why forge_spy_mean_rev's phantom from
4/30 was still visible in cluster_exposure on 5/24. The 5/31 reset
would have repeated the bug.

FIXED in this commit. `ops.maintenance.epoch_reset` now:
- Plans a `killed_state_clears` move for every strategy in
  `KILLED_STRATEGY_CUTOFFS` whose state.json has non-empty
  `open_trade` / `open_trades` / `open_positions` / `current_picks`
- Executes the clear (with pre-snapshot to archive manifest for
  rollback)
- Active (non-killed) strategy state files are NEVER touched
- 8 new tests pin this behavior

Dry-run today against current state correctly identifies both
phantoms: `forge_nq_overnight` (5/22 19:55 UTC) + `forge_spy_mean_rev`
(4/30). When you next run `python -m ops.maintenance.epoch_reset
--target <date> --execute`, those will be cleared.

SAFETY REQUIREMENT (unchanged): before running the reset, operator
must verify broker is actually flat for any killed-strategy symbol
(use `python -m ops.audit.run_orphan_phantom_check` to find candidates,
then check TWS Positions tab). The reset does not reconcile against
the broker; if a real position exists, the reset will create an
unmanaged record. emergency_close.py FIRST if needed.

---

## 2.3. Clear phantom positions from killed strategies (new — 2026-05-24)

The cluster_exposure scan finds two phantom positions consuming cluster
cap that should be at $0:

- `forge_nq_overnight`: open_trade @ 29529 × 1 MNQ from 2026-05-22
  19:55 UTC (the signal-only phantom from the 5/22 connect-fail —
  diagnosed earlier tonight)
- `forge_spy_mean_rev`: open_trade @ 718.39 × 1 SPY from 2026-04-30
  (ancient state from the original kill date)

Both strategies are in `KILLED_STRATEGY_CUTOFFS` (kill registry updated
this commit). The state files still hold open_trade dicts, which
`helio.cluster_exposure._scan_open_positions()` picks up and reports
as active exposure.

Run the detector:
```
python -m ops.audit.run_orphan_phantom_check
```

Remediation (operator):
1. Confirm broker is actually flat for MNQ and SPY (check TWS Positions tab)
2. If broker IS flat → state is stale; clear it with the one-liner
   the audit prints (sets `open_trade=null` in state.json)
3. If broker HAS the position → run `python -m emergency_close --symbol MNQ`
   etc. to actually flatten before clearing state. NEVER clear state
   without verifying broker, or you create an unmanaged position.
4. Re-run `python -m ops.audit.run_capacity_stress` to confirm cluster
   cap freed.

---

## 2.35. Capacity headroom constraint — cluster caps bind at 1× (new — 2026-05-24)

Capacity stress re-run with the surviving roster + $250K anchor reveals
that all 3 active strategies (xs_momentum, gld_pm_long, tom_spy) have
**max_safe_multiplier = 1.0×**. Meaning: scaling any active strategy
above its current configured cap will breach cluster/portfolio limits.

What this means in practice:
- Current paper roster at the current anchor is sized AT the binding
  constraint, not below it.
- A real-money flip can deploy AT the current size, but cannot scale
  up without raising cluster caps in `helio/cluster_exposure.py`.
- The preflight surfaces this as YELLOW on capacity_headroom_2x — not
  RED, because the strategy is fine AT current size; just not for
  expansion.

To raise the headroom (operator decision):
- Edit `helio/cluster_exposure.PER_CLUSTER_CAP_X` if cluster cap is
  the binding constraint
- OR edit `helio/cluster_exposure.SINGLE_INSTRUMENT_CAP_X` if the
  individual-symbol cap binds first
- Re-run `python -m ops.audit.run_capacity_stress` to verify

Until raised, preflight will stay YELLOW on capacity for all 3 active
strategies — which is acceptable for an initial small real-money
allocation but blocks scaling.

Audit artifact: `ops/reports/system_audit/capacity_stress.json`.

---

## 2.4. Run the real-money preflight before any allocation flip (new — 2026-05-24)

The 12-point real-money preflight is now executable. Run before any
decision to flip a strategy onto real money:

```
python -m ops.real_money_preflight --save
```

Current state (as of this commit):
- **forge_xs_momentum**: BLOCKED — 7 GREEN / 2 YELLOW / 3 RED. Closest
  to ready. Needs: 20+ live trades post-reset, real_money_allowlist
  flipped, capacity stress re-run.
- **forge_gld_pm_long**: BLOCKED — 6 GREEN / 2 YELLOW / 4 RED. Has a
  STRUCTURAL block (disciplined-gate FAIL at 5bp slippage, CI lower
  1.08 < 1.20 floor). Won't clear without strategy redesign.

Output:
- `ops/reports/system_audit/real_money_preflight.md` (markdown)
- `ops/reports/system_audit/real_money_preflight_<UTC>.json` (machine-readable)

Each strategy gets per-check GREEN/YELLOW/RED across:
  1. in_active_roster
  2. allocation_factor_positive
  3. disciplined_gate_passes (at realistic slippage)
  4. live_evidence_n (≥ 20 by default)
  5. live_pf_band (live verdict ≠ FAIL/WARNING)
  6. trade_source_is_live (not pre-reset archive)
  7. capacity_headroom_2x
  8. real_money_allowlist
  9. evidence_epoch_clean
  10. killed_strategy_invariant
  11. heartbeat_fresh (≤ 24h)
  12. halt_flag_absent

Exit codes: 0 if all READY_FOR_REAL, 1 if any BLOCKED_PENDING_REVIEW
(YELLOWs only), 2 if any BLOCKED (REDs).

---

## 2.5. Register the daily health-check cron (updated — 2026-05-24)

`ops/daily_health_check.py` is the canonical daily-cron entrypoint.
It composes everything tonight's safety work added into ONE scheduled
task: auto-pause recommendation, orphan-phantom detection, real-money
preflight, halt/flatten flag check. Posts ONE consolidated Discord
message if anything is RED or YELLOW; stays silent if all GREEN.

From an **Admin** PowerShell (replaces the ArgusAutoPause task that
was in earlier drafts of this doc):
```powershell
# Remove the old narrow auto_pause task if it was registered
Unregister-ScheduledTask -TaskName "ArgusAutoPause" -Confirm:$false -ErrorAction SilentlyContinue

$action = New-ScheduledTaskAction `
    -Execute "C:\Argus\.venv\Scripts\python.exe" `
    -Argument "-m ops.daily_health_check" `
    -WorkingDirectory "C:\Argus\repo"
$trigger = New-ScheduledTaskTrigger -Daily -At "10:00 PM"
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "ArgusDailyHealth" `
    -Action $action -Trigger $trigger -Settings $settings -Force
```

Daily at 22:00 local (post-market-close US), the task runs all four
checks. Output:
- stdout: human-readable summary
- `argus_flow/logs/daily_health_check.json`: machine-readable for
  later inspection
- Discord (only when not-all-GREEN): one combined message listing
  each RED/YELLOW component with a one-liner

Exit codes (cron-friendly):
- 0 if all GREEN
- 1 if any YELLOW (warning tier)
- 2 if any RED or ERROR (action required)

If you prefer the narrower auto-pause-only behavior, the old
`python -m ops.auto_pause` still works — daily_health_check just wraps
it as one of four components.

**Operator workflow when a PAUSE_RECOMMENDED alert fires**:

1. Read `argus_flow/logs/auto_pause_recommendations.json` to confirm
   the recommendation (verify it's not noise from a brief drawdown).
2. Dry-run the apply to see exactly what would change:
   ```
   python -m ops.auto_pause --apply
   ```
3. If you agree, apply for real:
   ```
   python -m ops.auto_pause --apply --confirm
   ```
4. This flips `allocation_factor=0.0` for every PAUSE_RECOMMENDED
   strategy, appends a line to `_kill_log` in allocation_factors.json,
   writes an event to `auto_pause_apply_events.jsonl`, and posts
   Discord confirmation.

The recommendation→apply split is intentional: live-PF-degradation
auto-pause has been wrong before (the 2026-05-23 silent-archive-fallback
bug made live_gate_monitor lie for 12 days). The human-in-the-loop
defends against systemic bugs in the signal we'd otherwise act on.

---

## 3. Register / verify scheduled tasks

After tonight's code changes, the dashboard + auto-watchdog should be
healthy, but two scheduled tasks need attention:

- `ArgusCohortReport`: was stuck in a stale "running" state during the
  audit. From an **Admin** PowerShell:
  ```
  Stop-ScheduledTask -TaskName "ArgusCohortReport" -ErrorAction SilentlyContinue
  Get-ScheduledTask -TaskName "ArgusCohortReport" | Select-Object TaskName, State
  ```
  Confirm State=Ready. If it's still in a wedged state, disable then
  re-enable.
- Dead `argus_flow.runner_unified` process: the FX runner was put down
  permanently 5/20 (3 argus FX pairs are slated for archive). If
  Get-Process shows it running, kill it — it's leftover from the old
  run. Use:
  ```
  Get-Process python | Where-Object { $_.CommandLine -match "runner_unified" } |
      Stop-Process
  ```
  Only do this if you confirm the CommandLine matches; do NOT kill
  every python process.

Optionally re-register the full task set via
`ops/register_tasks.ps1` (must be **Admin** PowerShell).

### NEW (2026-05-24): two scheduled tasks to register

Both come out of this session's Codex-gap closures. From an **Admin**
PowerShell:

**ArgusDailyHealth** — runs `daily_health_check`:
```
$action = New-ScheduledTaskAction -Execute 'C:\Argus\.venv\Scripts\python.exe' `
    -Argument '-m ops.daily_health_check' -WorkingDirectory 'C:\Argus\repo'
$trigger = New-ScheduledTaskTrigger -Daily -At 23:00
Register-ScheduledTask -TaskName 'ArgusDailyHealth' -Action $action -Trigger $trigger `
    -RunLevel Highest -Description '6-component daily health audit'
```

**ArgusDataFeedRefresh** — runs the new cache refresher (Codex gap #2):
```
$action = New-ScheduledTaskAction -Execute 'C:\Argus\.venv\Scripts\python.exe' `
    -Argument '-m ops.maintenance.refresh_data_feeds' -WorkingDirectory 'C:\Argus\repo'
$trigger = New-ScheduledTaskTrigger -Daily -At 22:00
Register-ScheduledTask -TaskName 'ArgusDataFeedRefresh' -Action $action -Trigger $trigger `
    -RunLevel Highest -Description 'Refresh yfinance CSV cache for daily-bar strategies'
```

Full operator playbook for the refresh task in
[`ops/DATA_FEED_RUNBOOK.md`](ops/DATA_FEED_RUNBOOK.md). Verify both
via `Get-ScheduledTask -TaskName 'Argus*' | Select TaskName,State`.

---

## 4. Verify nq_overnight stale state self-heals on next wake

On the first wake when TWS is reachable, the self-heal block I added
will clear the phantom `open_trade` from 5/22 16:00 UTC.

What you should see in `forge/logs/nq_overnight/runner.log`:
```
... [WARNING] nq_overnight: STALE_SIGNAL_ONLY: clearing phantom open_trade
  (entry_ts=2026-05-22 20:00:00+00:00, signal-only entry never had real
  broker position) so live signals can resume
```
After this line, `state.json` `open_trade` becomes `null` and the next
20:00 UTC signal hour will evaluate cleanly. If you don't see the
self-heal line, TWS connection failed and the runner stayed in fallback;
fix TWS first.

The same self-heal pattern was added defensively to gld_pm_long. It
doesn't have stale state today, but the next time IBKR drops mid-signal
it won't strand a phantom trade.

---

## 5. Verify xs_momentum will fire on June 1

I shipped `--loop` and `--evaluate` modes for `forge.xs_momentum`. The
rebalance gate fires only on the **first weekday wake of a new calendar
month** (with state['last_rebalance_month'] != current month). Manual
test you can run right now to confirm wiring:

```
python -m forge.xs_momentum.runner --evaluate
# Expected: {"action": "noop", "reason": "not_due (last_rebalance_month=)", ...}

python -m forge.xs_momentum.runner --check
# Expected: JSON with current top-2 picks from the broad-8 universe
```

To launch the daemon for production:
```
$env:IBKR_PORT = '7497'
Start-Process -FilePath 'C:\Argus\.venv\Scripts\python.exe' `
  -ArgumentList '-m','forge.xs_momentum.runner','--loop' `
  -WorkingDirectory 'C:\Argus\repo' -WindowStyle Hidden
```
Heartbeat at `forge/logs/xs_momentum/heartbeat.json` updates daily; the
real rebalance fires June 1 at 14:30 UTC (10:30 AM ET — first 60min of
the regular session, when the broad-8 ETFs have settled their opening
auctions).

If you want to force-rebalance immediately (e.g. to seed live evidence):
```
python -m forge.xs_momentum.runner --evaluate --force
```
This bypasses the month-boundary gate but still respects
allocation_factor (currently 1.0× for xs_momentum) and all real-money
boundary checks.

---

## 6. Verify allocation gate caught spy_trend_follower

I shipped `forge_spy_trend_follower` to read `allocation_factor` and
refuse to trade when it's 0.0 (the current setting). Sanity check:

```
python -m forge.spy_trend_follower.runner --evaluate --mode paper
# Expected log line:
# [ALLOC-GATE] action=... blocked: allocation_factor=0.0 (strategy deallocated)
```

If you want spy_trend_follower active later, edit
`argus_flow/configs/allocation_factors.json` and set its factor > 0.
The gate is read fresh on every cycle — no restart needed.

---

## 7. live_gate_monitor now tells the truth

Pre-fix: `live_gate_monitor` silently fell back to pre-5/22 archive
data when no live trades.csv existed, so `forge_gld_pm_long` appeared
to have n=16 live trades with PF 1.88. Those were April 2026 trades
from before the reset.

Post-fix: default `fallback_archive=False`, all 5 strategies correctly
report `INSUFFICIENT_N` with n=0 until real fills accumulate.

```
python -m helio.live_gate_monitor
# Expected: all 5 strategies show n=0, live_pf=n/a until trades fire
```

There is a regression test pinning this behavior:
`argus_flow/tests/test_live_gate_monitor.py::test_load_trades_silent_archive_fallback_disabled_by_default`

---

## 8. Promotion gate baseline updated with per-strategy slippage

`argus_flow/configs/promotion_gate_baseline.json` is now v2026-05-23.v2.
Every strategy now records:
- `slippage_bps_used`: what bps figure produced the published CI bounds
- `_slippage_bps_realistic`: Agent 2 5/23 recalibration

Strategies whose `_slippage_bps_realistic > slippage_bps_used` (notably
`forge_pead` at 10→40 bps and `forge_nq_overnight` at 3→7 bps) need
the gate re-run before any real-money consideration. Both are already
verdict=FAIL so this is informational, but it changes how aggressively
you should monitor for unexpected live PF — the bar is HIGHER than
the committed CI suggests for those two.

---

## What I shipped tonight (code, all already in the working tree)

| File | Change |
|---|---|
| [helio/live_gate_monitor.py](helio/live_gate_monitor.py) | silent archive fallback disabled by default; added `trade_source` field |
| [argus_flow/tests/test_live_gate_monitor.py](argus_flow/tests/test_live_gate_monitor.py) | 2 new regression tests (20 total, all green) |
| [forge/spy_trend_follower/runner.py](forge/spy_trend_follower/runner.py) | reads allocation_factor; fail-closed; refuses trade at 0.0 |
| [forge/xs_momentum/runner.py](forge/xs_momentum/runner.py) | `--evaluate` + `--loop` + `--force`; allocation gate; real-money boundary; canonical_fills writes; comment on auto_adjust=False rationale |
| [argus_flow/tests/test_xs_momentum_runner.py](argus_flow/tests/test_xs_momentum_runner.py) | 9 new control-flow tests for the rebalance gate + allocation fail-closed |
| [forge/nq_overnight/runner.py](forge/nq_overnight/runner.py) | self-heal: clears stale signal-only `open_trade` when broker confirms flat |
| [forge/gld_pm_long/runner.py](forge/gld_pm_long/runner.py) | same self-heal pattern (defensive — no stale state today) |
| [argus_flow/configs/promotion_gate_baseline.json](argus_flow/configs/promotion_gate_baseline.json) | v2 with per-strategy slippage_used + realistic; PEAD/NQ flagged for re-run |
| [helio/yfinance_data.py](helio/yfinance_data.py) | added repo-wide auto_adjust convention docblock |
| [ops/notify.py](ops/notify.py) | webhook loader now reads `secrets/discord.env` before falling back to `.env` |
| [ops/dashboard.py](ops/dashboard.py:486), [ops/dispatch_*.ps1](ops/), [ops/pull_pc2_results.ps1](ops/pull_pc2_results.ps1), [ops/check_pc2.ps1](ops/check_pc2.ps1), [ops/refresh_candles.ps1](ops/refresh_candles.ps1), [ops/status_all.ps1](ops/status_all.ps1), [ops/DISASTER_RECOVERY.md](ops/DISASTER_RECOVERY.md), [roadmap.txt](roadmap.txt) | PC2 IP 192.168.1.98 → 192.168.1.101 (9 live files; memory_backup snapshot intentionally left as-is) |

42 tests passing (xs_momentum runner: 9, xs_momentum pure logic: 13,
live_gate_monitor: 20).

---

## 1.48. Tuesday 2026-05-26 late evening — disabled ArgusManagedTruth scheduled task

**Symptom**: Discord alerts firing every 1-3 minutes through the evening:
- WARNING: `STALE for USD/JPY: runner=FLAT, broker=FLAT, hb_age=204617s, recon=CLEAN_FLAT`
- CRITICAL: `max managed-fleet reconciles exhausted - 3 attempts in the last hour. Manual intervention needed.`

**Root cause**: `ops/watchdog_managed.ps1` runs via `ArgusManagedTruth` scheduled task. It's a legacy watchdog from the pre-sunset era — it monitors argus FX trio (usdjpy/gbpusd/cadjpy) + Greek family (apollo/hermes/titan/ares) and tries to restart their runners on stale heartbeats. ALL of those strategies are in `helio.roi_filter.KILLED_STRATEGY_CUTOFFS` since the 2026-05-20 sunset batch. The watchdog has nothing legitimate to watch but was still firing alerts every hour about its "failures" to keep them alive.

**Resolution**: `Disable-ScheduledTask -TaskName 'ArgusManagedTruth'` + killed in-flight PID 26712. Fleet_monitor (`helio.fleet_monitor`) handles the current active fleet via the SYSTEMS dict; nothing else needs the legacy managed-truth watchdog.

**Re-enable conditions**: if any argus FX or Greek-family strategy is ever revived from the kill registry, also re-enable this task. Otherwise leave disabled.

