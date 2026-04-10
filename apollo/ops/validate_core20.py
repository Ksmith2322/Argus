"""Validate Core 20 watchlist: extended history, bear market, find replacements."""
import sys
from pathlib import Path
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import yfinance as yf
import numpy as np
import pandas as pd
from collections import defaultdict
import time

CORE_20 = ['AMD', 'MU', 'LRCX', 'MRNA', 'INTC', 'PLTR', 'TMO', 'GOOGL', 'ORCL', 'ARM',
           'SOFI', 'TSM', 'KLAC', 'PEP', 'CDNS', 'WMT', 'REGN', 'GILD', 'UPST', 'SNOW']

CANDIDATES = ['NFLX', 'AVGO', 'CRWD', 'PANW', 'FTNT', 'SHOP', 'DDOG', 'CRM',
              'LOW', 'HD', 'JPM', 'GS', 'MS', 'CAT', 'RTX', 'UPS',
              'SNPS', 'AMAT', 'MRVL', 'NVDA', 'META', 'MSFT', 'AMZN', 'AAPL']

def analyze_stock(sym, df_hist=None):
    ticker = yf.Ticker(sym)
    hist = ticker.earnings_history
    if hist is None or len(hist) < 3:
        return None

    df = ticker.history(period="5y", interval="1d", auto_adjust=True)
    if df.empty or len(df) < 200:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    close = df["Close"]

    quarters = []
    for q_date_raw, row in hist.iterrows():
        surprise = row.get("surprisePercent", 0)
        if surprise is None or np.isnan(surprise):
            continue
        q_date = q_date_raw.date() if hasattr(q_date_raw, "date") else q_date_raw

        mask_pre = close.index.date <= q_date
        mask_post = close.index.date > q_date
        if mask_pre.sum() < 5 or mask_post.sum() < 22:
            continue

        pre_close = float(close[close.index[mask_pre][-1]])
        day1_close = float(close[close.index[mask_post][0]])
        gap = (day1_close - pre_close) / pre_close * 100

        day20_close = float(close[close.index[mask_post][min(20, mask_post.sum() - 1)]])
        drift = (day20_close - day1_close) / day1_close * 100

        year = q_date.year
        is_bear = (year == 2022) or (year == 2025 and q_date.month <= 3)

        quarters.append({
            "date": str(q_date), "surprise": round(surprise * 100, 1),
            "beat": surprise > 0.01, "gap": round(gap, 1),
            "drift": round(drift, 1), "year": year, "is_bear": is_bear,
        })

    if not quarters:
        return None

    beats = [q for q in quarters if q["beat"]]
    beat_rate = len(beats) / len(quarters) * 100
    avg_gap_beat = np.mean([q["gap"] for q in beats]) if beats else 0
    avg_drift_beat = np.mean([q["drift"] for q in beats]) if beats else 0

    bear_beats = [q for q in quarters if q["is_bear"] and q["beat"]]
    bear_drift = np.mean([q["drift"] for q in bear_beats]) if bear_beats else 0
    bull_beats = [q for q in quarters if not q["is_bear"] and q["beat"]]
    bull_drift = np.mean([q["drift"] for q in bull_beats]) if bull_beats else 0

    return {
        "symbol": sym, "total_quarters": len(quarters),
        "beat_rate": round(beat_rate, 0),
        "avg_gap_beat": round(avg_gap_beat, 1),
        "avg_drift_beat": round(avg_drift_beat, 1),
        "total_edge": round(avg_gap_beat + avg_drift_beat, 1),
        "bull_drift": round(bull_drift, 1),
        "bear_drift": round(bear_drift, 1),
        "bear_quarters": len([q for q in quarters if q["is_bear"]]),
    }

# Analyze core 20
print("=" * 95)
print("CORE 20 EXTENDED VALIDATION (5yr history, bear market test)")
print("=" * 95)

results = []
for i, sym in enumerate(CORE_20):
    r = analyze_stock(sym)
    if r:
        results.append(r)
    if (i + 1) % 5 == 0:
        print(f"  {i+1}/{len(CORE_20)}...")

print(f"\n{'Symbol':>8s} {'Qtrs':>5s} {'Beat%':>6s} {'GapBt':>7s} {'DriftBt':>8s} {'Edge':>7s} {'BullDr':>7s} {'BearDr':>7s} {'BrQtr':>6s} {'Status':>10s}")
print("-" * 80)
for r in sorted(results, key=lambda x: x["total_edge"], reverse=True):
    if r["beat_rate"] >= 65 and r["total_edge"] >= 4 and r["bear_drift"] > -10:
        status = "KEEP"
    elif r["beat_rate"] < 60:
        status = "SWAP"
    elif r["total_edge"] < 2:
        status = "SWAP"
    elif r["bear_drift"] < -15:
        status = "BEAR_RISK"
    else:
        status = "MARGINAL"
    print(f"{r['symbol']:>8s} {r['total_quarters']:>5d} {r['beat_rate']:>5.0f}% {r['avg_gap_beat']:>+6.1f}% {r['avg_drift_beat']:>+7.1f}% {r['total_edge']:>+6.1f}% {r['bull_drift']:>+6.1f}% {r['bear_drift']:>+6.1f}% {r['bear_quarters']:>6d} {status:>10s}")

# Find replacements
print(f"\n{'=' * 95}")
print("REPLACEMENT CANDIDATES")
print("=" * 95)

candidates_list = [c for c in CANDIDATES if c not in CORE_20]
replacements = []
for i, sym in enumerate(candidates_list):
    r = analyze_stock(sym)
    if r and r["beat_rate"] >= 65 and r["total_edge"] >= 4:
        replacements.append(r)
    if (i + 1) % 5 == 0:
        print(f"  {i+1}/{len(candidates_list)}...")
    time.sleep(0.3)

print(f"\n{'Symbol':>8s} {'Qtrs':>5s} {'Beat%':>6s} {'GapBt':>7s} {'DriftBt':>8s} {'Edge':>7s} {'BullDr':>7s} {'BearDr':>7s}")
print("-" * 65)
for r in sorted(replacements, key=lambda x: x["total_edge"], reverse=True):
    print(f"{r['symbol']:>8s} {r['total_quarters']:>5d} {r['beat_rate']:>5.0f}% {r['avg_gap_beat']:>+6.1f}% {r['avg_drift_beat']:>+7.1f}% {r['total_edge']:>+6.1f}% {r['bull_drift']:>+6.1f}% {r['bear_drift']:>+6.1f}%")

# Final recommendation
print(f"\n{'=' * 95}")
print("FINAL RECOMMENDED WATCHLIST")
print("=" * 95)

keepers = [r["symbol"] for r in results if r["beat_rate"] >= 65 and r["total_edge"] >= 3]
swaps = [r["symbol"] for r in results if r["beat_rate"] < 60 or r["total_edge"] < 2]
adds = [r["symbol"] for r in replacements if r["total_edge"] >= 5]

print(f"  KEEP:     {keepers}")
print(f"  SWAP OUT: {swaps}")
print(f"  ADD:      {adds}")
final = [s for s in keepers if s not in swaps] + adds
print(f"  FINAL:    {sorted(final)} ({len(final)} stocks)")
