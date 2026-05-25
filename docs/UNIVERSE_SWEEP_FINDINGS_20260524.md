# Universe sweep findings — 2026-05-24

Two sweeps were run to answer "can we run our existing winners on a
broader pool of instruments to get more volume of evidence?"

## Sweep 1 — `forge_xs_momentum` cross-sectional ranker, 8 candidate universes

Method: replay the live `xs_momentum.backtest()` engine against each
universe, score with bootstrap-PF CI at 10bp round-trip slippage.
Survivor bar: PF CI lower >= 1.20 (OFFENSE role).

### Result: 4 of 8 universes PASS

| Universe | Size | n | PF | CI lower | CAGR | Max DD | Verdict |
|---|---|---|---|---|---|---|---|
| sectors_spdr_11 | 11 | 54 | 3.58 | 1.56 | 25.1% | 26.8% | **SURVIVOR** |
| commodities_7 | 7 | 39 | 1.41 | 0.57 | 4.1% | 60.6% | fail |
| countries_10 | 10 | 67 | 1.67 | 0.68 | 7.2% | 63.0% | fail |
| bonds_duration_7 | 7 | 35 | 0.74 | 0.23 | -0.7% | 21.4% | fail (loses money) |
| style_factors_8 | 8 | 38 | 5.09 | 1.96 | 28.9% | 12.8% | **SURVIVOR** ★ best DD |
| real_assets_7 | 7 | 26 | 4.69 | 1.50 | 20.0% | 22.7% | **SURVIVOR** |
| international_equity_8 | 8 | 56 | 1.71 | 0.88 | 5.4% | 34.3% | fail |
| legacy_sectors_countries_15 | 15 | 74 | 3.44 | 1.67 | 28.0% | 36.0% | **SURVIVOR** |

### Notable observations

1. **Sectors PASSES**. This contradicts the 5/22 finding that sector-only
   failed (CI lower 1.05). The 5/22 sweep used 20y history with stricter
   period stability; this sweep uses 10y. **Re-test at 20y before
   deploying to be sure the 4-year-OOS holds.**

2. **Style factors are the cleanest pass**: PF 5.09, CI lower 1.96, max
   DD only 12.8% (vs broad-8 baseline ~37%). If the gate result holds
   at 20y, this is a strictly-better deployment than broad-8.

3. **Real-assets and legacy 15-ticker also pass**. Real-assets has
   small n=26, so the CI is wide — needs more bars before confidence.

4. **Bonds, countries, commodities, international fail.** These don't
   have enough cross-asset dispersion to produce a momentum edge at
   realistic slippage.

### Recommended paper deployments

After 20y re-verification (the next step), deploy these as parallel
variants at 0.0× allocation (paper-evidence accumulation):

- `forge_xs_momentum_sectors_spdr_11`
- `forge_xs_momentum_style_factors_8` ← best risk-adjusted, highest priority
- `forge_xs_momentum_real_assets_7`

The legacy 15-ticker universe was the live default before the 5/22
swap and is documented as failing; **do NOT re-deploy** until the
contradiction with 5/22 is investigated.

## Sweep 2 — `forge_gld_pm_long` ATR-breakout signal, 13 candidate instruments

Method: replay the live 1h-bar ATR-breakout signal on a panel of 8
commodity ETFs + 5 sanity tickers (SPY/QQQ/TLT/XLE/XLB). Survivor bar:
PF CI lower >= 1.05 (DEFENSE role floor).

### Result: 0 of 13 PASS at 10bp slippage; 0 at 5bp slippage

| Ticker | n | PF @ 10bp | CI lo @ 10bp | PF @ 5bp | CI lo @ 5bp |
|---|---|---|---|---|---|
| GLD (baseline) | 530 | 0.53 | 0.42 | 0.95 | 0.78 |
| SLV | 535 | 0.93 | 0.74 | 1.24 | 1.00 (near miss) |
| GDX | 535 | 0.82 | 0.69 | 1.08 | 0.91 |
| USO | 521 | 0.92 | 0.75 | 1.25 | 1.03 (near miss) |
| UNG | 518 | 0.88 | 0.73 | 1.07 | 0.90 |
| DBC | 533 | 0.40 | 0.33 | 0.81 | 0.67 |
| DBA | 497 | 0.23 | 0.19 | 0.61 | 0.50 |
| IAU | 532 | 0.53 | 0.42 | 0.97 | 0.79 |
| SPY | 547 | 0.31 | 0.24 | 0.61 | 0.49 |
| QQQ | 536 | 0.44 | 0.36 | 0.74 | 0.60 |
| TLT | 540 | 0.21 | 0.17 | 0.53 | 0.44 |
| XLE | 530 | 0.44 | 0.36 | 0.67 | 0.55 |
| XLB | 535 | 0.31 | 0.25 | 0.55 | 0.45 |

### Findings

1. **The gld_pm_long ATR-breakout signal does NOT survive its own
   disciplined gate on its own universe** when replayed via this sweep
   (PF 0.53 @ 10bp, 0.95 @ 5bp on GLD). The current live disciplined-
   gate baseline (CI lower 1.08, per memory) was computed with a
   different methodology that this sweep does not match.

   **Action**: investigate the methodology discrepancy before treating
   any of these results as deployment signals. The most likely cause
   is the live baseline uses different history (longer window?) or
   different filters than `backtest()` exposes.

2. **No commodity-panel deployment is warranted**. Even at the live
   baseline's 5bp slippage assumption, none of SLV/GDX/USO/UNG/DBC/DBA
   crosses the DEFENSE floor of 1.05 (SLV and USO come close: 1.00,
   1.03).

3. **No equity/bond deployment is warranted either**. SPY/QQQ/TLT/
   XLE/XLB all fail strongly. The signal is not "any-instrument
   intraday continuation"; it's specific to whatever in-sample
   conditions the production-baseline window captured.

### gld_pm_long real-money implication

If the sweep methodology is correct, gld_pm_long should be considered
**at-risk** going into the 6/30 evidence window. If after 30 days of
live evidence the strategy's live PF doesn't track the production
baseline's CI band, the strategy should be demoted to OBSERVE or
killed. The live evidence is the tiebreaker, not the backtest.

## Recommended next actions

1. **Build `xs_momentum` paper-variant runners** for the 3 survivors
   (sectors / style / real-assets) at allocation 0.0× through 6/30.
   Same engine, different universe parameter. Tracks 3 parallel
   evidence streams.

2. **Re-test the 3 survivors at 20y** to verify the 4-year-OOS gap
   between 5/22's sector-fail result and tonight's sector-pass result.
   If the 20y test reaffirms PASS, proceed with deployment. If it
   reaffirms FAIL (consistent with 5/22), do NOT deploy.

3. **Investigate the gld_pm_long sweep / live-baseline methodology gap**.
   The sweep result is inconsistent with the production disciplined-
   gate baseline; that needs reconciliation before treating the
   live strategy as trustworthy.

4. **Do NOT deploy any gld_pm_long commodity companion**. Backtest
   says no.

## What the user gets out of this work

- 3-4 new live paper strategies (xs_momentum variants), each
  generating its own canonical_fills stream → 3-4× the evidence
  velocity for the same engine
- A red flag on `gld_pm_long` that the live evidence window should
  resolve one way or the other
- Reusable sweep tooling: any new universe candidate can be tested
  in <2 minutes; any new signal logic in 1 day
