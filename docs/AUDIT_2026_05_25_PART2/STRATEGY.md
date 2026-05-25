# Strategy Agent — New-Edge Candidates (Audit 2026-05-25 Part 2)

## Lead Recommendation (1-week implementation)

**Build `forge_overnight_drift_qqq` first.** US equity indices earn essentially all of their long-run risk-adjusted return between the prior 4pm close and the next 9:30 open (the "overnight drift" anomaly, documented in Kelly/Clark 2011, Lou/Polk/Skouras 2019, and replicated annually). It is independent of `forge_xs_momentum` (different factor: term-of-day, not cross-sectional rank), generates ~250 trades/year (n=20 in a month, n=2500 over 10y - the disciplined gate's appetite), and uses only the SPY/QQQ daily bars we already have. Honest expectation: PF 1.4-1.7 at 5bps slippage, Sharpe 0.7-1.0, max DD 15-22%; the failure mode (overnight risk premium compresses post-Reg-NMS) is exactly what bootstrap CI on the H1/H2 walk-forward halves will catch.

---

## Ranked Candidate Slate

### 1. `forge_overnight_drift_qqq` — Overnight Equity Risk Premium (PRIORITY)

- **Thesis.** The overnight session captures the institutional risk premium for holding US equity exposure across hours when markets are illiquid and macro news lands; intraday returns are roughly zero-mean once overnight is stripped. Capacity-constrained: each ETF crosses ~$10B/day at open auction, so a $1M sleeve never moves prints.
- **Universe.** SPY + QQQ + IWM (most-liquid US index ETFs). Three independent legs with a regime gate (only enter the leg whose 60-day overnight Sharpe > 0).
- **Rules.** ENTRY: MOC order at 15:55 ET, hold to 09:35 ET MOO open the next day. SIZING: vol-targeted to 10% annualized per leg (~$33K notional per $100K anchor split 3 ways).
- **Pre-backtest expectation.** PF 1.4-1.7 @ 5bps, CAGR +6 to +10%, max DD 15-22%, n ~2000-2500 trades over 10y per leg.
- **Failure mode.** Overnight premium compresses if global vol regime falls into 12-VIX state for >2y (rare but happened 2017); the rolling 60d-Sharpe regime gate handles this by going flat per-leg.
- **Time to ship.** 2 days to backtest + bootstrap + walk-forward; 1 day to wire runner (clone `forge/nov_spy/runner.py` skeleton, swap to 2 MOC/MOO orders).

### 2. `forge_turn_of_quarter` — Calendar Mandate-Flow Effect

- **Thesis.** Pension/401k contributions land at quarter-start; index funds must put cash to work in the first 3 trading days of each quarter. This produces predictable buy-pressure on SPY/MTUM, distinct from the monthly TOM effect (already running but at allocation 0). Documented by Etula et al. 2020.
- **Universe.** SPY only (focus the dispersion-poor signal). Optionally add MTUM as a 2nd leg for factor concentration.
- **Rules.** ENTRY: MOC of last trading day of quarter. EXIT: MOC of 3rd trading day of new quarter (≤4 trading-day hold). SIZING: full anchor × allocation_factor (calendar hold, no stops — same pattern as `forge_tom_spy`).
- **Pre-backtest expectation.** PF 1.8-2.5 at 5bps, n=80 trades over 20y (sparse but high-conviction), CAGR contribution 1.5-2.5% on full allocation, max DD 8-12%.
- **Failure mode.** n=80 is a thin sample — bootstrap CI lower will be the binding constraint, not point PF. If post-2020 buyback-pause cycles dampened the effect, halves will diverge.
- **Time to ship.** 1 day (literal fork of `forge/tom_spy/runner.py` with quarter-end calendar logic).

### 3. `forge_credit_spread_regime` — Credit-Stress Gated Equity Long

- **Thesis.** HYG/LQD spread widening leads SPY drawdowns by 2-6 weeks (Gilchrist-Zakrajsek excess bond premium literature). A simple long-SPY gated on `HYG/LQD > 50d MA` flips off precisely before equity stress. NOT a re-skin of xs_momentum: regime indicator is rates-side dispersion, not equity-side.
- **Universe.** SPY (long-side). Signal computed on HYG + LQD daily bars.
- **Rules.** ENTRY: long SPY when HYG/LQD ratio crosses above its 50d SMA AND VIX < 22. EXIT: when ratio crosses below 50d SMA (typically 30-90 day holds). SIZING: 1.0× anchor on engaged days; flat otherwise (~60-70% time-in-market).
- **Pre-backtest expectation.** PF 1.5-2.0 at 10bps, CAGR +7-9%, max DD ~12% (avoids 2020 March, 2022 H1, 2008 H2 by design), n=25-40 round-trips over 20y.
- **Failure mode.** Whipsaw in credit-equity decorrelation regimes (rare; ~2015-16); n=25 means bootstrap CI will be wide. Add a 5-day confirmation filter to halve the whipsaw rate.
- **Time to ship.** 3 days (new helio/credit_regime.py module + runner; 2-day backtest sweep).

### 4. `forge_xs_low_vol_factor` — Defensive-Factor Cross-Section

- **Thesis.** Low-volatility stocks earn the same long-run return as the market with materially lower drawdowns (Frazzini-Pedersen "Betting Against Beta"). NOT a re-skin of `xs_momentum` because the ranking signal is *inverse-vol*, not 12-1 return; uncorrelated to MOM factor.
- **Universe.** 6 ETFs: USMV, SPLV, EFAV, EEMV, ACWV, plus 1 high-beta short proxy SPHB used as a *signal-not-trade* indicator. Trade only the 5 long-only low-vol funds.
- **Rules.** ENTRY: monthly rebalance, hold the 2 ETFs with the lowest trailing 60-day realized volatility (already inverse-ranked vs vol so this picks the steadiest names). EXIT: monthly rebalance. SIZING: equal weight on 2 chosen tickers at 0.5× anchor each.
- **Pre-backtest expectation.** PF 1.5-1.8, CAGR +5-7%, max DD 12-18% (designed-in defensive bias), n ≈ 240 rebalance-trades over 10y.
- **Failure mode.** In a sustained low-vol bull (e.g. 2017), low-vol underperforms high-beta; CI will tighten on the H2 (2018-26) half. If the gate fails, the strategy is honest factor harvesting and doesn't add over buying USMV directly — same outcome as the audit's verdict on xs_momentum, but in a different (orthogonal) factor.
- **Time to ship.** 2 days (xs_momentum engine already exists; only the ranking function swaps from 12-1 return to inverse-vol).

### 5. `forge_yield_curve_macro_gate` — 2s10s Regime Overlay

- **Thesis.** When the 2s10s yield curve inverts, equity-risk-premium horizon shortens; long-equity positions sized down (or off) on inversion outperform on Sharpe (Estrella-Hardouvelis 1991, replicated through 2024). NOT a re-skin: the signal is a macro term-structure spread, not equity-side.
- **Universe.** Wrap an *existing* strategy (xs_momentum broad-8 baseline) rather than build a new tradeable: when 2s10s < 0 AND inverted for ≥10 consecutive trading days, multiply xs_momentum sizing by 0.5×.
- **Rules.** Data: yfinance `^TNX` and `^IRX` (or use FRED via existing data fetchers). ENTRY/EXIT: not a trade strategy — a *sizing overlay* in `helio/regime_gates.py`. Same hooks the existing 200dma regime uses.
- **Pre-backtest expectation.** Overlay reduces xs_momentum max DD from 37% to ~25%, costs ~1% CAGR (~16% → 15%), Sharpe improves +0.15.
- **Failure mode.** Inversion-led recessions are rare (n=4 since 1990) so the OOS gate test is genuinely thin; could be a curve-shape artifact rather than skill.
- **Time to ship.** 1 day (overlay only; no new runner — modify `xs_momentum/runner.py` regime block).

### 6. `forge_etf_pair_meanrev_xlk_xlf` — Sector-Spread Z-Score

- **Thesis.** XLK/XLF spread mean-reverts at z>2.5 in absence of major regime breaks (tech/financials are the two largest sectors, highly cointegrated since 2010 outside Q4-2018 and Q1-2023). Distinct from `coint_pairs` (KILLED) because it uses *one* highly liquid pair with regime-conditional entry, not 8 pairs averaged.
- **Universe.** XLK long / XLF short (dollar-neutral) OR the reverse, based on z-score sign.
- **Rules.** ENTRY: z(XLK_ret - XLF_ret, 90d) > 2.5 → short XLK / long XLF (and inverse for < -2.5). EXIT: z reverts to ±0.5 OR 30 trading days elapsed OR z > 4.0 stop. SIZING: 0.5× anchor per leg, fully hedged notional.
- **Pre-backtest expectation.** PF 1.3-1.7 at 10bps, CAGR contribution +3-5%, max DD 10-15%, n ≈ 30-50 trades over 10y (z>2.5 is rare).
- **Failure mode.** Tech-financials cointegration broke during the Mar-Apr 2023 regional-bank crisis (XLF -25% while XLK +15% in 6 weeks); n=50 means a single regime break dominates the H2 stats. Disciplined-gate H1/H2 split will be the honest filter.
- **Time to ship.** 2 days (small new pair runner; pattern exists in `forge/gdx_gld_pairs.py` to copy).

### 7. `forge_sell_in_may_modulated` — Halloween-Effect Sector Tilt

- **Thesis.** The "Halloween effect" (May-Oct underperformance vs Nov-Apr) is the most-replicated calendar anomaly in equity finance (Bouman-Jacobsen 2002, Andrade et al. 2013 across 109 markets). NOT a duplicate of `forge_nov_spy` (which holds only November); this is a 6-month *defensive-rotation* between SPY and SHY.
- **Universe.** SPY (Nov-Apr) ↔ SHY (May-Oct).
- **Rules.** ENTRY: MOC last trading day of Apr → switch SPY → SHY. ENTRY: MOC last trading day of Oct → switch SHY → SPY. SIZING: 1.0× anchor at all times (always invested).
- **Pre-backtest expectation.** PF 1.6-2.0 (calendar concentration), CAGR +7-9% vs SPY-bench +9%, max DD 18-22% (avoids 2008 Sept-Oct, 2020 Mar, 2022 H1), Sharpe +0.2 vs SPY.
- **Failure mode.** Halloween effect has weakened post-2010 (some studies show 0.2% remaining vs 1.0% pre-2000); H2 (2018-26) bootstrap may be the binding constraint. If it fails, the strategy is academic-but-stale and we drop it.
- **Time to ship.** 1 day (clone `nov_spy/runner.py`, expand to 2-state machine).

---

## Two Candidates I Would NOT Pursue

- **Anything intraday-microstructure (VWAP reversion, opening-range, volume anomalies).** The audit (Tier 2 roadmap) correctly identifies that we don't have tick data. Building an OR strategy on 5-minute yfinance bars will produce a backtest, but the slippage/queue-position assumptions are uncheckable, so the disciplined gate's 10bps slippage line is meaningless. Defer until tick ingestion ships (Tier 2 item #3).
- **Cross-asset carry overlays (currency carry, commodity term structure).** Currency carry requires FX rate-differential feeds the bot doesn't carry cleanly; commodity term-structure was already attempted (`forge_vix_carry`, KILLED). Both look great on a 2008-2021 backtest and then die when the carry-funding regime flips. The H1/H2 walk-forward is exactly the test that killed `vix_carry`; same fate likely.

---

## Closing Note on Independence

Of the seven candidates above, items 1, 2, 3, 5, 7 are factor-orthogonal to `xs_momentum` (term-of-day, calendar, credit, rates-shape, season). Items 4 and 6 are within-equity but use ranking signals (vol, pair-spread) uncorrelated to 12-1 momentum. **Even if only 2 of 7 survive the disciplined gate, the cohort gains genuine diversification** — moving the audit verdict from "5 of 6 are xs_momentum costumes" toward "3 independent engines" without requiring tick data or new feeds.

Implement #1 first; bundle #2 and #7 (1-day each) as the second sprint; reserve #3 as the headline-quality candidate that, if it passes, materially changes the audit's "no measurable alpha" verdict.
