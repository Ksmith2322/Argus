"""Index Futures Payoff-First Analysis — NQ, ES, YM + FX pairs.

Pulls data, finds displacement events, analyzes precursors,
tests trigger rules, runs payoff test, and stress tests.

Adapted for 1-minute bars with volume data (futures have real volume).

Usage:
    python -m argus_flow.analytics.index_futures_analysis
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

try:
    from ib_insync import IB, Future, Forex
except ImportError:
    print("ERROR: pip install ib_insync")
    sys.exit(1)


DATA_DIR = Path("argus_flow/data")
OUT_DIR = Path("argus_flow/replay_out/indices")
DATA_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)


# ── Data Pull ───────────────────────────────────────────────

def pull_futures_data(ib: IB, symbol: str, exchange: str, expiry: str, label: str) -> pd.DataFrame:
    """Pull max available 1-min data for a futures contract."""
    contract = Future(symbol=symbol, exchange=exchange, lastTradeDateOrContractMonth=expiry)
    qualified = ib.qualifyContracts(contract)
    if not qualified:
        print(f"  Could not qualify {symbol}")
        return pd.DataFrame()

    c = qualified[0]
    all_bars = []
    end_dt = ""

    for i in range(6):  # up to ~42 days
        try:
            bars = ib.reqHistoricalData(
                c, endDateTime=end_dt, durationStr="7 D",
                barSizeSetting="1 min", whatToShow="TRADES", useRTH=False,
                formatDate=1, timeout=30,
            )
            if not bars:
                break
            print(f"    Pull {i+1}: {len(bars)} bars ({bars[0].date} to {bars[-1].date})")
            all_bars = list(bars) + all_bars
            end_dt = bars[0].date.strftime("%Y%m%d %H:%M:%S")
            time.sleep(2)
        except Exception as e:
            print(f"    Pull {i+1}: {e}")
            break

    if not all_bars:
        return pd.DataFrame()

    df = pd.DataFrame([{
        "ts": str(b.date), "open": b.open, "high": b.high,
        "low": b.low, "close": b.close, "volume": b.volume,
    } for b in all_bars])

    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)

    out_path = DATA_DIR / f"ibkr_{label}_1m.csv"
    df.to_csv(out_path, index=False)
    print(f"  Saved {len(df):,} bars to {out_path}")
    return df


def pull_fx_data(ib: IB, pair: str) -> pd.DataFrame:
    """Pull max available 1-min data for an FX pair."""
    contract = Forex(pair)
    ib.qualifyContracts(contract)

    all_bars = []
    end_dt = ""

    for i in range(5):
        try:
            bars = ib.reqHistoricalData(
                contract, endDateTime=end_dt, durationStr="7 D",
                barSizeSetting="1 min", whatToShow="BID", useRTH=False,
                formatDate=1, timeout=30,
            )
            if not bars:
                break
            print(f"    Pull {i+1}: {len(bars)} bars ({bars[0].date} to {bars[-1].date})")
            all_bars = list(bars) + all_bars
            end_dt = bars[0].date.strftime("%Y%m%d %H:%M:%S")
            time.sleep(2)
        except Exception as e:
            print(f"    Pull {i+1}: {e}")
            break

    if not all_bars:
        return pd.DataFrame()

    df = pd.DataFrame([{
        "ts": str(b.date), "open": b.open, "high": b.high,
        "low": b.low, "close": b.close, "volume": b.volume,
    } for b in all_bars])

    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)

    out_path = DATA_DIR / f"ibkr_{pair.lower()}_1m.csv"
    df.to_csv(out_path, index=False)
    print(f"  Saved {len(df):,} bars to {out_path}")
    return df


# ── Analysis ────────────────────────────────────────────────

def prepare_features(df: pd.DataFrame) -> pd.DataFrame:
    """Add all features needed for analysis."""
    df = df.copy()
    df["bar_range"] = df["high"] - df["low"]
    df["bar_range_pct"] = df["bar_range"] / df["close"]

    lookback = 30
    ctx_win = 240

    # Volatility
    df["ctx_range_mean"] = df["bar_range"].rolling(ctx_win, min_periods=60).mean()
    df["pre_range_mean"] = df["bar_range"].rolling(lookback, min_periods=lookback).mean()
    df["vol_z"] = np.where(
        df["ctx_range_mean"] > 0,
        (df["pre_range_mean"] - df["ctx_range_mean"]) / df["ctx_range_mean"],
        0,
    )

    # Range compression
    df["pre_high"] = df["high"].rolling(lookback, min_periods=lookback).max()
    df["pre_low"] = df["low"].rolling(lookback, min_periods=lookback).min()
    df["range_pct"] = (df["pre_high"] - df["pre_low"]) / df["close"]

    # Range acceleration
    df["br_first"] = df["bar_range"].shift(15).rolling(15, min_periods=15).mean()
    df["br_last"] = df["bar_range"].rolling(15, min_periods=15).mean()
    df["range_accel"] = np.where(df["br_first"] > 0, (df["br_last"] - df["br_first"]) / df["br_first"], 0)

    # Volume features (futures have real volume)
    if df["volume"].sum() > 0:
        df["vol_mean"] = df["volume"].rolling(ctx_win, min_periods=60).mean()
        df["vol_pre"] = df["volume"].rolling(lookback, min_periods=lookback).mean()
        df["volume_z"] = np.where(df["vol_mean"] > 0, (df["vol_pre"] - df["vol_mean"]) / df["vol_mean"], 0)

        # Volume acceleration
        df["vol_first"] = df["volume"].shift(15).rolling(15, min_periods=15).mean()
        df["vol_last"] = df["volume"].rolling(15, min_periods=15).mean()
        df["vol_accel"] = np.where(df["vol_first"] > 0, (df["vol_last"] - df["vol_first"]) / df["vol_first"], 0)

        # 5-min volume burst
        df["vol_5m"] = df["volume"].rolling(5, min_periods=5).sum()
        df["vol_5m_mean"] = df["volume"].rolling(5, min_periods=5).sum().rolling(ctx_win, min_periods=60).mean()
        df["vol_burst_z"] = np.where(
            df["vol_5m_mean"] > 0,
            (df["vol_5m"] - df["vol_5m_mean"]) / df["vol_5m_mean"],
            0,
        )
    else:
        df["volume_z"] = 0
        df["vol_accel"] = 0
        df["vol_burst_z"] = 0

    # Session position
    df["ctx_high"] = df["high"].rolling(ctx_win, min_periods=60).max()
    df["ctx_low"] = df["low"].rolling(ctx_win, min_periods=60).min()
    df["ctx_range_abs"] = df["ctx_high"] - df["ctx_low"]
    df["dist_from_low"] = np.where(
        df["ctx_range_abs"] > 0,
        (df["close"] - df["ctx_low"]) / df["ctx_range_abs"],
        0.5,
    )

    df["hour"] = df["ts"].dt.hour

    return df


def find_displacement_events(df, horizon_bars, threshold_pct, min_gap=None):
    """Find non-overlapping displacement events."""
    if min_gap is None:
        min_gap = horizon_bars

    fwd_h = df["high"].shift(-1).rolling(horizon_bars, min_periods=1).max().shift(-horizon_bars + 1)
    fwd_l = df["low"].shift(-1).rolling(horizon_bars, min_periods=1).min().shift(-horizon_bars + 1)
    up = (fwd_h - df["close"]) / df["close"]
    down = (df["close"] - fwd_l) / df["close"]

    events = []
    next_allowed = 0
    for i in range(len(df)):
        if i < next_allowed:
            continue
        u = up.iloc[i] if not pd.isna(up.iloc[i]) else 0
        d = down.iloc[i] if not pd.isna(down.iloc[i]) else 0
        if u >= threshold_pct or d >= threshold_pct:
            events.append({
                "idx": i, "ts": df.at[i, "ts"], "close": float(df.at[i, "close"]),
                "direction": "up" if u >= d else "down",
                "magnitude": float(max(u, d)),
            })
            next_allowed = i + min_gap
    return pd.DataFrame(events)


def precursor_contrast(df, events, n_background_mult=3, lookback=30):
    """Compare features before events vs random background."""
    features = ["vol_z", "range_pct", "range_accel", "volume_z", "vol_accel", "vol_burst_z", "dist_from_low"]
    available = [f for f in features if f in df.columns and df[f].notna().any()]

    precursors = []
    for _, ev in events.iterrows():
        idx = int(ev["idx"])
        if idx < lookback + 10:
            continue
        row = {f: float(df.at[idx, f]) for f in available if not pd.isna(df.at[idx, f])}
        row["magnitude"] = ev["magnitude"]
        row["idx"] = idx
        precursors.append(row)

    if not precursors:
        return {}, pd.DataFrame()

    pdf = pd.DataFrame(precursors)

    # Background
    rng = np.random.default_rng(42)
    event_idx = set(pdf["idx"].values)
    candidates = [i for i in range(lookback + 10, len(df) - 60) if i not in event_idx]
    n_bg = min(len(pdf) * n_background_mult, len(candidates))
    bg_idx = rng.choice(candidates, size=n_bg, replace=False)

    bg_data = []
    for idx in bg_idx:
        row = {f: float(df.at[idx, f]) for f in available if not pd.isna(df.at[idx, f])}
        bg_data.append(row)
    bgdf = pd.DataFrame(bg_data)

    results = {}
    for feat in available:
        if feat in ["magnitude", "idx"]:
            continue
        if feat not in pdf.columns or feat not in bgdf.columns:
            continue
        ev_mean = pdf[feat].mean()
        bg_mean = bgdf[feat].mean()
        pooled = np.sqrt((pdf[feat].std() ** 2 + bgdf[feat].std() ** 2) / 2)
        d = abs(ev_mean - bg_mean) / pooled if pooled > 0 else 0
        strength = "STRONG" if d > 0.5 else ("MEDIUM" if d > 0.3 else "weak")
        results[feat] = {"cohens_d": d, "strength": strength, "ev_med": float(pdf[feat].median()), "bg_med": float(bgdf[feat].median())}

    return results, pdf


def simulate_trades(df, signal_indices, stop_pct, target_pct, max_hold, fee_rt=0.00002):
    """Simulate trades with stop/target/timeout."""
    results = []
    for idx in signal_indices:
        if idx >= len(df) - 1:
            continue
        entry_px = float(df.at[idx, "close"])
        dist_low = float(df.at[idx, "dist_from_low"]) if "dist_from_low" in df.columns else 0.5

        direction = "long" if dist_low < 0.4 else ("short" if dist_low > 0.6 else "long")

        if direction == "long":
            stop_px = entry_px * (1 - stop_pct)
            target_px = entry_px * (1 + target_pct)
        else:
            stop_px = entry_px * (1 + stop_pct)
            target_px = entry_px * (1 - target_pct)

        outcome = "timeout"
        exit_px = entry_px
        for j in range(1, max_hold + 1):
            bi = idx + j
            if bi >= len(df):
                break
            bar = df.iloc[bi]
            if direction == "long":
                if bar["low"] <= stop_px:
                    outcome, exit_px = "stop", stop_px
                    break
                if bar["high"] >= target_px:
                    outcome, exit_px = "target", target_px
                    break
            else:
                if bar["high"] >= stop_px:
                    outcome, exit_px = "stop", stop_px
                    break
                if bar["low"] <= target_px:
                    outcome, exit_px = "target", target_px
                    break

        if outcome == "timeout":
            last_bi = min(idx + max_hold, len(df) - 1)
            exit_px = float(df.at[last_bi, "close"])

        if direction == "long":
            raw_pnl = (exit_px - entry_px) / entry_px
        else:
            raw_pnl = (entry_px - exit_px) / entry_px

        results.append({"idx": idx, "direction": direction, "outcome": outcome,
                        "raw_pnl_pct": raw_pnl, "net_pnl_pct": raw_pnl - fee_rt})

    return pd.DataFrame(results)


def find_signals(df, rule_fn, min_gap=15):
    """Find signal bars matching rule, with minimum gap."""
    valid = df.dropna(subset=["range_pct", "range_accel", "vol_z"])
    if "volume_z" in df.columns:
        valid = valid.dropna(subset=["volume_z"])
    matches = valid[valid.apply(rule_fn, axis=1)]
    deduped = []
    next_allowed = 0
    for idx in matches.index:
        if idx >= next_allowed:
            deduped.append(idx)
            next_allowed = idx + min_gap
    return pd.Index(deduped)


def run_full_analysis(df, label, n_days, point_value=1.0, fee_per_contract=0.62):
    """Run complete payoff-first analysis on one instrument."""
    print(f"\n{'#'*80}")
    print(f"  {label} — {len(df):,} bars, {n_days:.1f} days")
    print(f"{'#'*80}")

    df = prepare_features(df)
    has_volume = df["volume"].sum() > 0

    # Phase 1: Displacement events
    print(f"\n--- Phase 1: Displacement Events ---")
    # Use percentage thresholds that make sense for this instrument
    avg_px = df["close"].mean()
    for h, t_pct, t_label in [(5, 0.002, "20bps/5m"), (15, 0.003, "30bps/15m"), (30, 0.005, "50bps/30m"), (60, 0.0075, "75bps/60m")]:
        events = find_displacement_events(df, h, t_pct)
        if len(events) > 0:
            pts = t_pct * avg_px
            print(f"  {t_label} (~{pts:.1f}pts): {len(events)} events ({len(events)/n_days:.1f}/day)")

    # Use 30bps/15min as the target (good balance for indices)
    target_events = find_displacement_events(df, 15, 0.003)
    if len(target_events) < 10:
        target_events = find_displacement_events(df, 30, 0.005)
    if len(target_events) < 10:
        target_events = find_displacement_events(df, 15, 0.002)

    print(f"\n  Target events for analysis: {len(target_events)} ({len(target_events)/n_days:.1f}/day)")

    # Phase 2: Precursor contrast
    print(f"\n--- Phase 2: Precursor Contrast ---")
    contrast, precursor_df = precursor_contrast(df, target_events)
    if contrast:
        strong = [f for f, v in contrast.items() if v["strength"] == "STRONG"]
        medium = [f for f, v in contrast.items() if v["strength"] == "MEDIUM"]
        weak = [f for f, v in contrast.items() if v["strength"] == "weak"]

        for feat, v in sorted(contrast.items(), key=lambda x: x[1]["cohens_d"], reverse=True):
            print(f"    {feat:>15s}: d={v['cohens_d']:.3f} ({v['strength']})")

        print(f"\n  STRONG: {strong or 'none'}")
        print(f"  MEDIUM: {medium or 'none'}")
    else:
        print("  No precursor data available")
        strong, medium = [], []

    # Phase 3: Trigger rules and payoff test
    print(f"\n--- Phase 3: Trigger Rules + Payoff Test ---")

    # Build rules based on what features are available and strong
    triggers = {
        "range+accel": lambda r: r["range_pct"] >= 0.0012 and r["range_accel"] > 0,
        "range+accel+session": lambda r: r["range_pct"] >= 0.0012 and r["range_accel"] > 0 and 13 <= r["hour"] <= 20,
    }

    if has_volume:
        triggers["range+vol"] = lambda r: r["range_pct"] >= 0.0012 and r["range_accel"] > 0 and r.get("volume_z", 0) > 0
        triggers["full+vol"] = lambda r: r["range_pct"] >= 0.0012 and r["range_accel"] > 0 and r.get("volume_z", 0) > 0 and 13 <= r["hour"] <= 20
        triggers["vol_burst"] = lambda r: r.get("vol_burst_z", 0) > 1.0 and r["range_accel"] > 0 and 13 <= r["hour"] <= 20
        triggers["vol_burst+range"] = lambda r: r.get("vol_burst_z", 0) > 0.5 and r["range_pct"] >= 0.0015 and r["range_accel"] > 0

    # Fee as percentage of price
    # For micro futures, commission is ~$0.62/contract
    # MNQ: $2/pt, so $0.62 = 0.31 pts = ~0.0013% at 24000
    # MES: $5/pt, so $0.62 = 0.124 pts = ~0.0019% at 6500
    # For simplicity use a conservative 3bps round trip
    fee_rt = 0.00003  # 3bps for futures

    stop_targets = [
        (0.0010, 0.0020, 15),
        (0.0015, 0.0030, 30),
        (0.0020, 0.0040, 60),
        (0.0020, 0.0040, 30),
        (0.0025, 0.0050, 60),
        (0.0030, 0.0060, 60),
    ]

    all_results = []

    for tname, rule in triggers.items():
        signals = find_signals(df, rule, min_gap=15)
        if len(signals) < 10:
            continue

        print(f"\n  Trigger: {tname} ({len(signals)} signals, {len(signals)/n_days:.1f}/day)")
        print(f"    {'SL':>5} {'TP':>5} {'H':>4} | {'n':>4} {'WR':>6} {'tgt%':>6} {'stp%':>6} {'tmo%':>6} | {'exp%':>8} {'v':>5}")
        print(f"    {'-'*60}")

        for s, t, h in stop_targets:
            rdf = simulate_trades(df, signals, s, t, h, fee_rt=fee_rt)
            if rdf.empty:
                continue
            wr = (rdf["net_pnl_pct"] > 0).mean()
            tgt = (rdf["outcome"] == "target").mean()
            stp = (rdf["outcome"] == "stop").mean()
            tmo = (rdf["outcome"] == "timeout").mean()
            exp = rdf["net_pnl_pct"].mean()
            exp_pts = exp * avg_px
            v = "YES" if exp > 0 else "no"

            sp = s * 10000
            tp = t * 10000
            print(f"    {sp:>4.0f}b {tp:>4.0f}b {h:>3}m | {len(rdf):>4} {wr:>6.3f} {tgt:>5.1%} {stp:>5.1%} {tmo:>5.1%} | {exp*10000:>+7.2f}b {v:>5}")

            all_results.append({
                "instrument": label, "trigger": tname,
                "stop_bps": sp, "target_bps": tp, "max_hold": h,
                "n": len(rdf), "win_rate": float(wr),
                "target_rate": float(tgt), "stop_rate": float(stp),
                "exp_bps": float(exp * 10000), "viable": exp > 0,
            })

    # Summary
    viable = [r for r in all_results if r["viable"]]
    print(f"\n  === {label} SUMMARY ===")
    print(f"  Configs tested: {len(all_results)}")
    print(f"  Viable: {len(viable)}")

    if viable:
        viable.sort(key=lambda x: x["exp_bps"], reverse=True)
        best = viable[0]
        print(f"  Best: {best['trigger']} SL={best['stop_bps']:.0f}b TP={best['target_bps']:.0f}b H={best['max_hold']}m")
        print(f"         exp={best['exp_bps']:+.2f}bps WR={best['win_rate']:.3f} n={best['n']}")
        daily = best["n"] / n_days
        annual_bps = best["exp_bps"] * daily * 252
        print(f"         {daily:.1f} trades/day, ~{annual_bps:+.0f} bps/year")

    return all_results


def main():
    ib = IB()
    ib.connect("127.0.0.1", 4002, clientId=90, timeout=10)
    print("Connected to IBKR")

    instruments = [
        # (symbol, exchange, expiry, label, type)
        ("MNQ", "CME", "20260618", "MNQ_nasdaq_micro", "future"),
        ("MES", "CME", "20260618", "MES_sp500_micro", "future"),
        ("MYM", "CBOT", "20260618", "MYM_dow_micro", "future"),
        ("NQ", "CME", "20260618", "NQ_nasdaq_emini", "future"),
    ]

    fx_pairs = ["GBPUSD", "USDJPY"]

    all_results = []

    # Pull and analyze futures
    for symbol, exchange, expiry, label, itype in instruments:
        print(f"\n{'='*80}")
        print(f"Pulling {label}...")
        csv_path = DATA_DIR / f"ibkr_{label}_1m.csv"

        if csv_path.exists():
            print(f"  Using cached data: {csv_path}")
            df = pd.read_csv(csv_path)
            df["ts"] = pd.to_datetime(df["ts"], utc=True)
        else:
            df = pull_futures_data(ib, symbol, exchange, expiry, label)

        if df.empty:
            print(f"  No data for {label}, skipping")
            continue

        n_days = (df["ts"].max() - df["ts"].min()).total_seconds() / 86400
        results = run_full_analysis(df, label, n_days)
        all_results.extend(results)

    # Pull and analyze FX
    for pair in fx_pairs:
        print(f"\n{'='*80}")
        print(f"Pulling {pair}...")
        csv_path = DATA_DIR / f"ibkr_{pair.lower()}_1m.csv"

        if csv_path.exists():
            print(f"  Using cached data: {csv_path}")
            df = pd.read_csv(csv_path)
            df["ts"] = pd.to_datetime(df["ts"], utc=True)
        else:
            df = pull_fx_data(ib, pair)

        if df.empty:
            print(f"  No data for {pair}, skipping")
            continue

        n_days = (df["ts"].max() - df["ts"].min()).total_seconds() / 86400
        results = run_full_analysis(df, pair, n_days)
        all_results.extend(results)

    ib.disconnect()

    # Grand summary
    print(f"\n\n{'='*80}")
    print("GRAND SUMMARY — ALL INSTRUMENTS")
    print(f"{'='*80}")

    viable = [r for r in all_results if r["viable"]]
    print(f"\nTotal configs tested: {len(all_results)}")
    print(f"Total viable: {len(viable)}")

    if viable:
        viable.sort(key=lambda x: x["exp_bps"], reverse=True)
        print(f"\nTOP VIABLE CONFIGS:")
        print(f"  {'Instrument':>20s} {'Trigger':>20s} {'SL':>5} {'TP':>5} {'H':>4} {'n':>5} {'WR':>6} {'exp_bps':>8}")
        print(f"  {'-'*75}")
        for r in viable[:20]:
            print(f"  {r['instrument']:>20s} {r['trigger']:>20s} {r['stop_bps']:>4.0f}b {r['target_bps']:>4.0f}b {r['max_hold']:>3}m {r['n']:>5} {r['win_rate']:>6.3f} {r['exp_bps']:>+7.2f}b")

    # Save
    pd.DataFrame(all_results).to_csv(OUT_DIR / "all_results.csv", index=False)
    (OUT_DIR / "summary.json").write_text(json.dumps({
        "total_configs": len(all_results),
        "viable": len(viable),
        "results": all_results,
    }, indent=2, default=str))
    print(f"\nSaved: {OUT_DIR / 'all_results.csv'}")


if __name__ == "__main__":
    main()