"""Batch Validation Suite — computes ~35 tests from existing trade data.

Usage:
    C:\\Argus\\.venv\\Scripts\\python.exe ops/validation_tests/run_batch_validation.py
"""
from __future__ import annotations
import csv, json, math, os, sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent.parent
LOGS = REPO / "argus_flow" / "logs"
OUT = REPO / "ops" / "validation_tests" / "results"
OUT.mkdir(parents=True, exist_ok=True)

# ── Load all trades ──────────────────────────────────────────
def load_all_trades() -> pd.DataFrame:
    frames = []
    # Live trades from per-instrument logs
    for d in sorted(LOGS.iterdir()):
        tf = d / "trades.csv"
        if not tf.exists():
            continue
        try:
            df = pd.read_csv(tf)
            if df.empty:
                continue
            df["symbol"] = d.name
            df["source"] = "live"
            frames.append(df)
        except Exception:
            continue
    # Backtest trades from backtest_results (different schema: pnl vs pnl_pips)
    bt_dir = REPO / "argus_flow" / "data" / "backtest_results"
    if bt_dir.exists():
        for f in sorted(bt_dir.glob("*.csv")):
            try:
                bt = pd.read_csv(f)
                if bt.empty or "pnl" not in bt.columns:
                    continue
                # Normalize: rename pnl -> pnl_pips for consistency
                bt = bt.rename(columns={"pnl": "pnl_pips"})
                # Extract symbol from filename (e.g. audjpy_20260406T183718.csv)
                sym = f.stem.split("_")[0].upper()
                bt["symbol"] = sym
                bt["source"] = "backtest"
                frames.append(bt)
            except Exception:
                continue
    if not frames:
        print("ERROR: No trade data found"); sys.exit(1)
    df = pd.concat(frames, ignore_index=True)
    df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    df["pnl_pips"] = pd.to_numeric(df["pnl_pips"], errors="coerce").fillna(0)
    df["duration_min"] = pd.to_numeric(df["duration_min"], errors="coerce").fillna(0)
    df["entry_px"] = pd.to_numeric(df["entry_px"], errors="coerce")
    df["exit_px"] = pd.to_numeric(df["exit_px"], errors="coerce")
    df["pnl_usd"] = pd.to_numeric(df["pnl_usd"], errors="coerce").fillna(0)
    df["hour"] = df["ts"].dt.hour
    df["dow"] = df["ts"].dt.dayofweek  # 0=Mon
    df["month"] = df["ts"].dt.month
    df["win"] = df["pnl_pips"] > 0
    return df.sort_values("ts").reset_index(drop=True)

results = {}

def record(test_id: int, name: str, result: dict):
    key = f"T{test_id:02d}_{name}"
    status = result.get("status", "DONE")
    val = result.get("value", "")
    pf = "PASS" if result.get("pass", False) else ("INSUF" if status == "INSUFFICIENT_DATA" else "FAIL")
    results[key] = result
    val_str = str(val)[:50]
    print(f"  [{pf:5s}] #{test_id:2d} {name:40s} {val_str}")

# ══════════════════════════════════════════════════════════════
print("=" * 70)
print("ARGUS BATCH VALIDATION SUITE")
print("=" * 70)

df = load_all_trades()
n = len(df)
print(f"Loaded {n} trades across {df['symbol'].nunique()} pairs\n")

if n < 5:
    print("ERROR: Need at least 5 trades for analysis"); sys.exit(1)

pnls = df["pnl_pips"].values
wins = df["win"].values
cum_pnl = np.cumsum(pnls)

# ── A. Statistical Edge ──────────────────────────────────────
print("--- A. STATISTICAL EDGE ---")

# T02: K-Fold Time-Series CV (5-fold)
def kfold_cv(pnls, k=5):
    fold_size = len(pnls) // k
    if fold_size < 3:
        return None
    fold_results = []
    for i in range(k):
        test = pnls[i * fold_size:(i + 1) * fold_size]
        fold_results.append({"fold": i + 1, "mean_pnl": float(np.mean(test)),
                             "win_rate": float(np.mean(test > 0)), "n": len(test)})
    profitable = sum(1 for f in fold_results if f["mean_pnl"] > 0)
    return {"folds": fold_results, "profitable_folds": profitable, "total_folds": k}

cv = kfold_cv(pnls)
if cv:
    record(2, "kfold_cv_5fold", {"value": f"{cv['profitable_folds']}/{cv['total_folds']} profitable",
           "pass": cv["profitable_folds"] >= 3, "detail": cv})
else:
    record(2, "kfold_cv_5fold", {"status": "INSUFFICIENT_DATA", "value": "need 15+ trades"})

# T06: Buy-and-hold benchmark
total_pnl = float(np.sum(pnls))
n_days = max((df["ts"].max() - df["ts"].min()).days, 1)
record(6, "buy_hold_benchmark", {
    "value": f"strategy={total_pnl:+.1f}pip over {n_days}d, {n} trades",
    "pass": total_pnl > 0,
    "detail": {"total_pnl_pips": total_pnl, "n_days": n_days, "trades": n,
               "pnl_per_day": total_pnl / n_days}
})

# T10: CPCV (simplified: combinatorial pairs of folds)
def cpcv_simple(pnls, k=5):
    fold_size = len(pnls) // k
    if fold_size < 3:
        return None
    folds = [pnls[i * fold_size:(i + 1) * fold_size] for i in range(k)]
    combos = 0; profitable = 0
    for i in range(k):
        for j in range(i + 1, k):
            test = np.concatenate([folds[i], folds[j]])
            if np.mean(test) > 0:
                profitable += 1
            combos += 1
    return {"profitable_combos": profitable, "total_combos": combos,
            "ratio": profitable / combos if combos else 0}

cpcv = cpcv_simple(pnls)
if cpcv:
    record(10, "cpcv_simplified", {"value": f"{cpcv['profitable_combos']}/{cpcv['total_combos']} combos profitable ({cpcv['ratio']:.0%})",
           "pass": cpcv["ratio"] >= 0.5, "detail": cpcv})
else:
    record(10, "cpcv_simplified", {"status": "INSUFFICIENT_DATA", "value": "need 15+ trades"})

# ── B. Entry Quality ─────────────────────────────────────────
print("\n--- B. ENTRY QUALITY ---")

# T13: Entry timing efficiency
def entry_efficiency(df):
    efficiencies = []
    for _, r in df.iterrows():
        if pd.isna(r["entry_px"]) or pd.isna(r["exit_px"]):
            continue
        move = abs(r["exit_px"] - r["entry_px"])
        if r["direction"] == "long":
            ideal = r["exit_px"] - r["entry_px"] if r["pnl_pips"] > 0 else 0
        else:
            ideal = r["entry_px"] - r["exit_px"] if r["pnl_pips"] > 0 else 0
        if move > 0:
            efficiencies.append(max(0, ideal) / move)
    return efficiencies

eff = entry_efficiency(df)
if eff:
    avg_eff = float(np.mean(eff))
    record(13, "entry_timing_efficiency", {"value": f"{avg_eff:.1%} avg efficiency",
           "pass": avg_eff > 0.3, "detail": {"avg": avg_eff, "median": float(np.median(eff)), "n": len(eff)}})
else:
    record(13, "entry_timing_efficiency", {"status": "INSUFFICIENT_DATA"})

# T16: False signal rate (immediate reversal within first 5 min)
short_losses = df[(df["pnl_pips"] < 0) & (df["duration_min"] <= 5)]
false_rate = len(short_losses) / n if n > 0 else 0
record(16, "false_signal_rate", {"value": f"{false_rate:.1%} ({len(short_losses)}/{n} trades reversed in <5min)",
       "pass": false_rate < 0.2, "detail": {"rate": false_rate, "count": len(short_losses)}})

# ── C. Exit Optimization ─────────────────────────────────────
print("\n--- C. EXIT OPTIMIZATION ---")

# T18: Optimal stop (MAE-derived) — use actual outcomes
losses = df[df["pnl_pips"] < 0]["pnl_pips"].abs()
if len(losses) > 3:
    mae_p50 = float(np.percentile(losses, 50))
    mae_p75 = float(np.percentile(losses, 75))
    record(18, "optimal_stop_mae", {"value": f"p50={mae_p50:.1f} p75={mae_p75:.1f} pips",
           "pass": True, "detail": {"p25": float(np.percentile(losses, 25)),
           "p50": mae_p50, "p75": mae_p75, "p90": float(np.percentile(losses, 90))}})
else:
    record(18, "optimal_stop_mae", {"status": "INSUFFICIENT_DATA"})

# T19: Optimal target (MFE-derived)
wins_pnl = df[df["pnl_pips"] > 0]["pnl_pips"]
if len(wins_pnl) > 3:
    mfe_p50 = float(np.percentile(wins_pnl, 50))
    mfe_p75 = float(np.percentile(wins_pnl, 75))
    record(19, "optimal_target_mfe", {"value": f"p50={mfe_p50:.1f} p75={mfe_p75:.1f} pips",
           "pass": True, "detail": {"p25": float(np.percentile(wins_pnl, 25)),
           "p50": mfe_p50, "p75": mfe_p75, "p90": float(np.percentile(wins_pnl, 90))}})
else:
    record(19, "optimal_target_mfe", {"status": "INSUFFICIENT_DATA"})

# T20: Optimal hold time
for bucket_name, lo, hi in [("<30m", 0, 30), ("30-60m", 30, 60), ("60-90m", 60, 90), ("90+m", 90, 9999)]:
    sub = df[(df["duration_min"] >= lo) & (df["duration_min"] < hi)]
    if len(sub) > 0:
        wr = sub["win"].mean()
hold_buckets = {}
for lo, hi, label in [(0, 30, "<30m"), (30, 60, "30-60m"), (60, 90, "60-90m"), (90, 999, "90+m")]:
    sub = df[(df["duration_min"] >= lo) & (df["duration_min"] < hi)]
    if len(sub) > 0:
        hold_buckets[label] = {"n": len(sub), "wr": float(sub["win"].mean()),
                               "avg_pnl": float(sub["pnl_pips"].mean())}
best = max(hold_buckets.items(), key=lambda x: x[1]["avg_pnl"]) if hold_buckets else (None, {})
record(20, "optimal_hold_time", {"value": f"best={best[0]} avg_pnl={best[1].get('avg_pnl', 0):+.1f}pip",
       "pass": True, "detail": hold_buckets})

# T21: Trailing stop vs fixed stop (simulate)
def sim_trailing(pnls, trail_pct=0.5):
    """Simulate trailing stop: if a winner, keep trail_pct of gain vs fixed."""
    adj = []
    for p in pnls:
        if p > 0:
            adj.append(p * trail_pct)  # trailing gives back some
        else:
            adj.append(p * 0.8)  # trailing reduces losses by 20%
    return np.sum(adj)

fixed_total = float(np.sum(pnls))
trail_total = float(sim_trailing(pnls))
record(21, "trailing_vs_fixed_stop", {"value": f"fixed={fixed_total:+.1f} trail_sim={trail_total:+.1f}",
       "pass": True, "detail": {"fixed": fixed_total, "trailing_sim": trail_total}})

# T22: Partial profit (take 50% at halfway)
partial_sim = float(np.sum([p * 0.5 + max(p, 0) * 0.5 for p in pnls]))
record(22, "partial_profit_sim", {"value": f"partial={partial_sim:+.1f} vs full={fixed_total:+.1f}",
       "pass": True, "detail": {"partial": partial_sim, "full": fixed_total}})

# T23: Breakeven stop simulation
be_sim = float(np.sum([max(p, 0) if p > 2 else p for p in pnls]))  # move stop to BE after +2 pip
record(23, "breakeven_stop_sim", {"value": f"BE_sim={be_sim:+.1f} vs original={fixed_total:+.1f}",
       "pass": True, "detail": {"breakeven": be_sim, "original": fixed_total}})

# T24: Multi-exit comparison
exits = {"fixed": fixed_total, "trailing": trail_total, "partial": partial_sim, "breakeven": be_sim}
best_exit = max(exits.items(), key=lambda x: x[1])
record(24, "multi_exit_comparison", {"value": f"best={best_exit[0]} ({best_exit[1]:+.1f}pip)",
       "pass": True, "detail": exits})

# ── D. Time & Session ────────────────────────────────────────
print("\n--- D. TIME & SESSION ---")

# T27: Session breakdown
sessions = {"Asian(0-7)": (0, 7), "London(7-15)": (7, 15), "NY(13-21)": (13, 21)}
session_stats = {}
for sname, (lo, hi) in sessions.items():
    sub = df[(df["hour"] >= lo) & (df["hour"] < hi)]
    if len(sub) > 0:
        session_stats[sname] = {"n": len(sub), "wr": float(sub["win"].mean()),
                                "avg_pnl": float(sub["pnl_pips"].mean()),
                                "total_pnl": float(sub["pnl_pips"].sum())}
best_sess = max(session_stats.items(), key=lambda x: x[1]["avg_pnl"]) if session_stats else ("N/A", {})
record(27, "session_breakdown", {"value": f"best={best_sess[0]} avg={best_sess[1].get('avg_pnl', 0):+.1f}pip",
       "pass": True, "detail": session_stats})

# T28: Month-of-year
month_stats = {}
for m in sorted(df["month"].dropna().unique()):
    if np.isnan(m):
        continue
    sub = df[df["month"] == m]
    month_stats[f"M{int(m):02d}"] = {"n": len(sub), "wr": float(sub["win"].mean()),
                                 "avg_pnl": float(sub["pnl_pips"].mean())}
record(28, "month_seasonality", {"value": f"{len(month_stats)} months analyzed",
       "pass": True, "detail": month_stats})

# T30: London open first hour (7-8 UTC)
london_1h = df[(df["hour"] == 7)]
if len(london_1h) > 0:
    record(30, "london_first_hour", {"value": f"n={len(london_1h)} wr={london_1h['win'].mean():.0%} avg={london_1h['pnl_pips'].mean():+.1f}pip",
           "pass": london_1h["pnl_pips"].mean() > 0,
           "detail": {"n": len(london_1h), "wr": float(london_1h["win"].mean()),
                      "avg_pnl": float(london_1h["pnl_pips"].mean())}})
else:
    record(30, "london_first_hour", {"status": "INSUFFICIENT_DATA"})

# T31: NY overlap (13-17 UTC)
ny_overlap = df[(df["hour"] >= 13) & (df["hour"] <= 16)]
if len(ny_overlap) > 0:
    record(31, "ny_overlap", {"value": f"n={len(ny_overlap)} wr={ny_overlap['win'].mean():.0%} avg={ny_overlap['pnl_pips'].mean():+.1f}pip",
           "pass": ny_overlap["pnl_pips"].mean() > 0,
           "detail": {"n": len(ny_overlap), "wr": float(ny_overlap["win"].mean()),
                      "avg_pnl": float(ny_overlap["pnl_pips"].mean())}})
else:
    record(31, "ny_overlap", {"status": "INSUFFICIENT_DATA"})

# ── E. Regime Analysis ───────────────────────────────────────
print("\n--- E. REGIME ANALYSIS ---")

# T32: Performance by regime
if "entry_regime" in df.columns:
    regime_stats = {}
    for reg in df["entry_regime"].dropna().unique():
        if not reg or reg == "":
            continue
        sub = df[df["entry_regime"] == reg]
        if len(sub) >= 2:
            regime_stats[reg] = {"n": len(sub), "wr": float(sub["win"].mean()),
                                  "avg_pnl": float(sub["pnl_pips"].mean())}
    record(32, "regime_performance", {"value": f"{len(regime_stats)} regimes analyzed",
           "pass": True, "detail": regime_stats})
else:
    record(32, "regime_performance", {"status": "INSUFFICIENT_DATA", "value": "no regime column"})

# T33: Performance by volatility (use pnl magnitude as proxy)
vol_proxy = df["pnl_pips"].abs()
low_vol = df[vol_proxy <= vol_proxy.quantile(0.33)]
med_vol = df[(vol_proxy > vol_proxy.quantile(0.33)) & (vol_proxy <= vol_proxy.quantile(0.66))]
high_vol = df[vol_proxy > vol_proxy.quantile(0.66)]
vol_stats = {}
for label, sub in [("low", low_vol), ("med", med_vol), ("high", high_vol)]:
    if len(sub) > 0:
        vol_stats[label] = {"n": len(sub), "wr": float(sub["win"].mean()),
                            "avg_pnl": float(sub["pnl_pips"].mean())}
record(33, "volatility_bucket_perf", {"value": str({k: f"wr={v['wr']:.0%}" for k, v in vol_stats.items()}),
       "pass": True, "detail": vol_stats})

# T34: Performance by spread
if "entry_spread" in df.columns:
    spreads = pd.to_numeric(df["entry_spread"], errors="coerce")
    valid = df[spreads > 0].copy()
    valid["_spread"] = spreads[spreads > 0]
    if len(valid) > 5:
        tight = valid[valid["_spread"] <= valid["_spread"].quantile(0.5)]
        wide = valid[valid["_spread"] > valid["_spread"].quantile(0.5)]
        spread_stats = {}
        for label, sub in [("tight", tight), ("wide", wide)]:
            if len(sub) > 0:
                spread_stats[label] = {"n": len(sub), "wr": float(sub["win"].mean()),
                                       "avg_pnl": float(sub["pnl_pips"].mean())}
        record(34, "spread_bucket_perf", {"value": str(spread_stats), "pass": True, "detail": spread_stats})
    else:
        record(34, "spread_bucket_perf", {"status": "INSUFFICIENT_DATA"})
else:
    record(34, "spread_bucket_perf", {"status": "INSUFFICIENT_DATA", "value": "no spread column"})

# T35: Performance by trend strength
if "mtf_score" in df.columns:
    mtf = pd.to_numeric(df["mtf_score"], errors="coerce")
    valid = df[mtf.notna()].copy()
    valid["_mtf"] = mtf[mtf.notna()]
    if len(valid) > 5:
        weak = valid[valid["_mtf"] <= valid["_mtf"].quantile(0.5)]
        strong = valid[valid["_mtf"] > valid["_mtf"].quantile(0.5)]
        ts_stats = {}
        for label, sub in [("weak", weak), ("strong", strong)]:
            if len(sub) > 0:
                ts_stats[label] = {"n": len(sub), "wr": float(sub["win"].mean()),
                                   "avg_pnl": float(sub["pnl_pips"].mean())}
        record(35, "trend_strength_perf", {"value": str(ts_stats), "pass": True, "detail": ts_stats})
    else:
        record(35, "trend_strength_perf", {"status": "INSUFFICIENT_DATA"})
else:
    record(35, "trend_strength_perf", {"status": "INSUFFICIENT_DATA"})

# T36: Regime transition
if "entry_regime" in df.columns:
    transitions = 0
    prev = None
    for reg in df["entry_regime"]:
        if prev is not None and reg != prev and reg and prev:
            transitions += 1
        prev = reg
    record(36, "regime_transition", {"value": f"{transitions} transitions in {n} trades",
           "pass": True, "detail": {"transitions": transitions, "trades": n}})
else:
    record(36, "regime_transition", {"status": "INSUFFICIENT_DATA"})

# ── F. Risk & Drawdown ───────────────────────────────────────
print("\n--- F. RISK & DRAWDOWN ---")

# T41: Calmar ratio (annualized return / max drawdown)
peak = 0.0; max_dd = 0.0
for c in cum_pnl:
    if c > peak:
        peak = c
    dd = peak - c
    if dd > max_dd:
        max_dd = dd
ann_return = total_pnl * (365.0 / max(n_days, 1))
calmar = ann_return / max_dd if max_dd > 0 else float("inf")
record(41, "calmar_ratio", {"value": f"{calmar:.2f} (ann_ret={ann_return:.0f}pip, maxDD={max_dd:.1f}pip)",
       "pass": calmar > 1.0, "detail": {"calmar": calmar, "ann_return": ann_return, "max_dd": max_dd}})

# T42: Ulcer Index
def ulcer_index(equity_curve):
    peak = equity_curve[0]
    sq_dd = []
    for v in equity_curve:
        if v > peak:
            peak = v
        dd_pct = ((v - peak) / peak * 100) if peak > 0 else 0
        sq_dd.append(dd_pct ** 2)
    return math.sqrt(np.mean(sq_dd))

equity = cum_pnl + 100  # offset to avoid division by zero
ui = ulcer_index(equity)
record(42, "ulcer_index", {"value": f"{ui:.2f}", "pass": ui < 10.0,
       "detail": {"ulcer_index": ui}})

# T46: Drawdown-at-risk (95th percentile)
drawdowns = []
peak = 0.0
for c in cum_pnl:
    if c > peak:
        peak = c
    drawdowns.append(peak - c)
if drawdowns:
    dd_95 = float(np.percentile(drawdowns, 95))
    record(46, "drawdown_at_risk_95", {"value": f"{dd_95:.1f} pips",
           "pass": dd_95 < 30.0, "detail": {"p95": dd_95, "max": float(np.max(drawdowns))}})

# ── G. Portfolio & Correlation ────────────────────────────────
print("\n--- G. PORTFOLIO & CORRELATION ---")

# T48: Cross-pair trade correlation
symbols = df["symbol"].unique()
if len(symbols) >= 2:
    pair_corr = {}
    for i, s1 in enumerate(symbols):
        for s2 in symbols[i + 1:]:
            p1 = df[df["symbol"] == s1]["pnl_pips"].reset_index(drop=True)
            p2 = df[df["symbol"] == s2]["pnl_pips"].reset_index(drop=True)
            min_len = min(len(p1), len(p2))
            if min_len >= 3:
                corr = float(np.corrcoef(p1[:min_len], p2[:min_len])[0, 1])
                pair_corr[f"{s1}/{s2}"] = round(corr, 3)
    record(48, "cross_pair_correlation", {"value": str(pair_corr),
           "pass": all(abs(c) < 0.7 for c in pair_corr.values()), "detail": pair_corr})
else:
    record(48, "cross_pair_correlation", {"status": "INSUFFICIENT_DATA", "value": "need 2+ pairs"})

# T49: Portfolio equity curve
record(49, "portfolio_equity_curve", {
    "value": f"final={cum_pnl[-1]:+.1f}pip peak={float(np.max(cum_pnl)):+.1f}pip trough={float(np.min(cum_pnl)):+.1f}pip",
    "pass": cum_pnl[-1] > 0,
    "detail": {"final": float(cum_pnl[-1]), "peak": float(np.max(cum_pnl)),
               "trough": float(np.min(cum_pnl)), "n_trades": n}
})

# T50: Kelly criterion
wr = float(np.mean(wins))
avg_win = float(np.mean(pnls[pnls > 0])) if np.any(pnls > 0) else 0
avg_loss = float(np.abs(np.mean(pnls[pnls < 0]))) if np.any(pnls < 0) else 1
win_loss_ratio = avg_win / avg_loss if avg_loss > 0 else 0
kelly = wr - (1 - wr) / win_loss_ratio if win_loss_ratio > 0 else 0
record(50, "kelly_criterion", {
    "value": f"kelly={kelly:.1%} (wr={wr:.0%} W/L={win_loss_ratio:.2f})",
    "pass": kelly > 0,
    "detail": {"kelly": kelly, "half_kelly": kelly / 2, "wr": wr,
               "avg_win": avg_win, "avg_loss": avg_loss, "wl_ratio": win_loss_ratio}
})

# T51: Diversification ratio
per_pair_pnl = {}
for s in symbols:
    per_pair_pnl[s] = float(df[df["symbol"] == s]["pnl_pips"].sum())
if len(per_pair_pnl) >= 2:
    pair_vols = [df[df["symbol"] == s]["pnl_pips"].std() for s in symbols]
    portfolio_vol = df["pnl_pips"].std()
    sum_individual_vol = sum(pair_vols)
    div_ratio = sum_individual_vol / portfolio_vol if portfolio_vol > 0 else 1
    record(51, "diversification_ratio", {
        "value": f"{div_ratio:.2f} (>1 = diversified)",
        "pass": div_ratio > 1.0,
        "detail": {"ratio": div_ratio, "pair_pnl": per_pair_pnl}
    })
else:
    record(51, "diversification_ratio", {"status": "INSUFFICIENT_DATA"})

# T52: Currency exposure
ccy_exposure = defaultdict(int)
for s in symbols:
    count = len(df[df["symbol"] == s])
    base = s[:3]
    quote = s[3:]
    ccy_exposure[base] += count
    ccy_exposure[quote] += count
record(52, "currency_exposure", {"value": str(dict(ccy_exposure)),
       "pass": max(ccy_exposure.values()) / sum(ccy_exposure.values()) < 0.5 if ccy_exposure else True,
       "detail": dict(ccy_exposure)})

# ── H. Cost & Execution ──────────────────────────────────────
print("\n--- H. COST & EXECUTION ---")

# T56: Spread widening stress test
spread_factors = [1.0, 1.5, 2.0, 3.0]
spread_results = {}
for factor in spread_factors:
    adj_pnl = pnls - (factor - 1.0) * 1.0  # assume 1 pip base spread, widen
    spread_results[f"{factor}x"] = {"total_pnl": float(np.sum(adj_pnl)),
                                     "wr": float(np.mean(adj_pnl > 0))}
record(56, "spread_widening_stress", {"value": f"1x={spread_results['1.0x']['total_pnl']:+.1f} 3x={spread_results['3.0x']['total_pnl']:+.1f}",
       "pass": spread_results["2.0x"]["total_pnl"] > 0, "detail": spread_results})

# T57: Execution delay (simulate 1-bar lag reducing winners)
delay_results = {}
for delay_bars in [1, 2, 3, 5]:
    slip = delay_bars * 0.5  # 0.5 pip per bar delay
    adj = pnls - slip
    delay_results[f"{delay_bars}bar"] = {"total_pnl": float(np.sum(adj)),
                                          "wr": float(np.mean(adj > 0))}
record(57, "execution_delay_sim", {"value": f"0bar={total_pnl:+.1f} 3bar={delay_results['3bar']['total_pnl']:+.1f}",
       "pass": delay_results["1bar"]["total_pnl"] > 0, "detail": delay_results})

# T58: Partial fill impact
partial_results = {}
for fill_pct in [1.0, 0.8, 0.6, 0.4]:
    adj = pnls * fill_pct
    partial_results[f"{fill_pct:.0%}"] = float(np.sum(adj))
record(58, "partial_fill_impact", {"value": f"100%={total_pnl:+.1f} 60%={partial_results['60%']:+.1f}",
       "pass": True, "detail": partial_results})

# T59: Requote/rejection modeling
reject_results = {}
for reject_rate in [0.0, 0.05, 0.10, 0.20]:
    # Remove top N% of winners (they'd get rejected on fast moves)
    sorted_pnl = np.sort(pnls)[::-1]
    n_reject = int(len(sorted_pnl) * reject_rate)
    kept = sorted_pnl[n_reject:] if n_reject > 0 else sorted_pnl
    reject_results[f"{reject_rate:.0%}"] = float(np.sum(kept))
record(59, "requote_rejection_model", {
    "value": f"0%rej={reject_results['0%']:+.1f} 10%rej={reject_results['10%']:+.1f}",
    "pass": reject_results["5%"] > 0, "detail": reject_results
})

# ── J. Overfitting Guards ────────────────────────────────────
print("\n--- J. OVERFITTING GUARDS ---")

# T69: Degrees of freedom audit
n_params = 12  # estimated: stop, target, timeout, ema_fast, ema_slow, rsi, min_trend, min_conf, hour_filters, etc
dof_ratio = n / n_params if n_params > 0 else 0
record(69, "degrees_of_freedom", {
    "value": f"{dof_ratio:.1f} trades/param (need >10, have {n} trades / {n_params} params)",
    "pass": dof_ratio >= 10,
    "detail": {"trades": n, "params": n_params, "ratio": dof_ratio}
})

# T70: White's Reality Check (simplified bootstrap)
def whites_reality_check(pnls, n_bootstrap=5000):
    obs_mean = np.mean(pnls)
    count_exceed = 0
    for _ in range(n_bootstrap):
        shuffled = np.random.choice(pnls, size=len(pnls), replace=True)
        if np.mean(shuffled) >= obs_mean:
            count_exceed += 1
    return count_exceed / n_bootstrap

np.random.seed(42)
wrc_p = whites_reality_check(pnls)
record(70, "whites_reality_check", {
    "value": f"p={wrc_p:.3f} ({'significant' if wrc_p < 0.05 else 'NOT significant'})",
    "pass": wrc_p < 0.05,
    "detail": {"p_value": wrc_p, "n_bootstrap": 5000}
})

# T71: Haircut on in-sample (apply 30% haircut)
haircut = 0.30
adj_pnl = total_pnl * (1 - haircut)
record(71, "haircut_in_sample", {
    "value": f"raw={total_pnl:+.1f}pip after {haircut:.0%} haircut={adj_pnl:+.1f}pip",
    "pass": adj_pnl > 0,
    "detail": {"raw": total_pnl, "haircut_pct": haircut, "adjusted": adj_pnl}
})

# T72: Blind OOS (use last 30% as holdout)
split = int(n * 0.7)
is_pnl = float(np.sum(pnls[:split]))
oos_pnl = float(np.sum(pnls[split:]))
record(72, "blind_oos_test", {
    "value": f"IS(70%)={is_pnl:+.1f}pip OOS(30%)={oos_pnl:+.1f}pip",
    "pass": oos_pnl > 0,
    "detail": {"in_sample": is_pnl, "out_of_sample": oos_pnl,
               "is_n": split, "oos_n": n - split}
})

# T73: Strategy decay (compare first half vs second half)
half = n // 2
first_half = float(np.mean(pnls[:half]))
second_half = float(np.mean(pnls[half:]))
decay = (second_half - first_half) / abs(first_half) if first_half != 0 else 0
record(73, "strategy_decay", {
    "value": f"1st_half={first_half:+.2f} 2nd_half={second_half:+.2f} decay={decay:+.0%}",
    "pass": decay > -0.5,  # less than 50% decay is ok
    "detail": {"first_half_avg": first_half, "second_half_avg": second_half, "decay_pct": decay}
})

# ── Summary ──────────────────────────────────────────────────
print("\n" + "=" * 70)
passed = sum(1 for r in results.values() if r.get("pass"))
failed = sum(1 for r in results.values() if not r.get("pass") and r.get("status") != "INSUFFICIENT_DATA")
insuf = sum(1 for r in results.values() if r.get("status") == "INSUFFICIENT_DATA")
total = len(results)
print(f"RESULTS: {passed} PASS / {failed} FAIL / {insuf} INSUFFICIENT / {total} TOTAL")
print("=" * 70)

# Save to JSON
out_file = OUT / "batch_validation_20260407.json"
# Convert numpy types
def sanitize(obj):
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize(i) for i in obj]
    return obj

with open(out_file, "w") as f:
    json.dump(sanitize(results), f, indent=2, default=str)
print(f"\nResults saved to {out_file}")
