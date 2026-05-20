"""forge.pead — Post-Earnings Announcement Drift, long-only.

STATUS (2026-05-19): RESEARCH → PAPER-CANDIDATE. Allocation 0.0 through
5/31 reset (pre-freeze epoch is contaminated; can't activate against it).
Eligible for activation in the post-reset window with the surviving fleet.

5-year backtest result (2021-2026, Apollo 16-name universe):
  n=69 trades  WR=37.7%  PF=2.04  avg_win=+17.3%/avg_loss=-5.1%
  scaled equity growth (15% position sizing) = +38.5% over 5y
  CAGR scaled = +7.47%  max DD scaled = 11.5%
  Per-ticker spread: 11 of 16 names produced >=3 trades; top contributors
  PLTR(9)/REGN(8)/MU(7)/GOOGL(6)/TSM(6). Result not carried by 1-2 names.
  Exit mix: 42 atr_stop (61%) + 27 hold_period_end (39%) — healthy.

HONEST CAVEAT: Apollo's universe was curated BECAUSE these names had
historical PEAD consistency. This backtest validates the IMPLEMENTATION,
not the factor in general. Generalizing to a random SPX universe would
require a separate test. Treat the +7.47% CAGR as upper-bound on the
edge until validated out-of-sample on a non-curated universe.

Strategy thesis (Architect audit 2026-05-19 + apollo team 2026-04-09):
  Bernard-Thomas 1989+: stocks that report earnings surprises drift in
  the direction of the surprise for ~1-60 days. Magnitude is correlated
  with the surprise size, volume on announcement day, and gap direction
  at next-day open.

  Implementation:
    - Universe: 16 names Apollo has tagged with ≥67% historical drift
      consistency on positive surprises (tier_1: GOOGL/KLAC/PEP/WMT;
      tier_2: MU/LRCX/MRNA/PLTR/SOFI/TSM/GILD; tier_3: AMD/INTC/TMO/ARM/REGN).
    - Entry: surprise ≥ 5%, announce-day volume ≥ 1.5× 30d avg, gap ≥ 0%.
    - Position: long the underlying at next-day open.
    - Exit: 20-trading-day hold OR -1.5 × 14d ATR stop, whichever first.
    - Frequency: 5-15 entries/quarter across the universe.

Pure decision logic lives in helio.pead_signal (tests + composability).
This package owns the runner + backtest harness + per-strategy state."""
