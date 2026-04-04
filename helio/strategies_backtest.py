"""Greek Family POC Backtests — Hermes, Apollo, Artemis on current pairs.

Tests all three new strategy families on the same instruments we already trade.
Uses daily/4hr data for Hermes, 1hr data for Apollo/Artemis.
"""
import pandas as pd
import numpy as np
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"


def load_daily(symbol):
    df = pd.read_csv(DATA_DIR / f"{symbol}_daily.csv", parse_dates=["Date"])
    df = df.set_index("Date").sort_index()
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna()


def load_hourly(symbol):
    path = DATA_DIR / f"{symbol}_1h.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path, parse_dates=["Date"])
    df = df.set_index("Date").sort_index()
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna()


def compute_metrics(trades):
    if not trades:
        return None
    pnls = [t["pnl_pct"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gw = sum(wins)
    gl = abs(sum(losses))
    pf = gw / gl if gl > 0 else 99
    wr = len(wins) / len(pnls)
    total = sum(pnls)

    equity = [0]
    for p in pnls:
        equity.append(equity[-1] + p)
    peak = 0
    max_dd = 0
    for e in equity:
        peak = max(peak, e)
        max_dd = max(max_dd, peak - e)

    exits = {}
    for t in trades:
        exits[t["exit_reason"]] = exits.get(t["exit_reason"], 0) + 1

    return {
        "trades": len(trades), "pf": round(pf, 3), "wr": round(wr, 3),
        "total": round(total, 2), "avg": round(total / len(pnls), 3),
        "max_dd": round(max_dd, 2), "exits": exits,
    }


# ═══════════════════════════════════════════════════════════════
# HERMES — Momentum/Breakout
# ═══════════════════════════════════════════════════════════════

def hermes_backtest(symbol, consolidation_bars=10, breakout_atr_mult=0.5,
                    vol_mult=1.5, atr_stop=1.5, atr_target=3.0, max_hold=24):
    """Momentum breakout: trade when price breaks consolidation range with volume."""
    df = load_daily(symbol)
    if len(df) < 60:
        return []

    df["atr"] = df["High"].sub(df["Low"]).rolling(14).mean()
    df["avg_vol"] = df["Volume"].rolling(20).mean()

    trades = []
    position = None

    for i in range(consolidation_bars + 20, len(df)):
        row = df.iloc[i]
        atr = row["atr"]

        if position is None:
            # Check for consolidation: last N bars have range < breakout_atr_mult * ATR
            window = df.iloc[i - consolidation_bars:i]
            window_range = window["High"].max() - window["Low"].min()
            consolidation = window_range < atr * consolidation_bars * breakout_atr_mult

            if not consolidation:
                continue

            range_high = window["High"].max()
            range_low = window["Low"].min()

            # Volume surge
            vol_ok = row["Volume"] > row["avg_vol"] * vol_mult

            # Breakout
            if row["Close"] > range_high and vol_ok:
                stop = row["Close"] - atr * atr_stop
                target = row["Close"] + atr * atr_target
                position = {
                    "direction": "LONG", "entry": row["Close"],
                    "stop": stop, "target": target, "bars": 0,
                    "entry_date": df.index[i],
                }
            elif row["Close"] < range_low and vol_ok:
                stop = row["Close"] + atr * atr_stop
                target = row["Close"] - atr * atr_target
                position = {
                    "direction": "SHORT", "entry": row["Close"],
                    "stop": stop, "target": target, "bars": 0,
                    "entry_date": df.index[i],
                }
        else:
            position["bars"] += 1
            exit_reason = None
            exit_price = row["Close"]

            if position["direction"] == "LONG":
                if row["Low"] <= position["stop"]:
                    exit_reason = "stop"; exit_price = position["stop"]
                elif row["High"] >= position["target"]:
                    exit_reason = "target"; exit_price = position["target"]
                elif position["bars"] >= max_hold:
                    exit_reason = "timeout"
            else:
                if row["High"] >= position["stop"]:
                    exit_reason = "stop"; exit_price = position["stop"]
                elif row["Low"] <= position["target"]:
                    exit_reason = "target"; exit_price = position["target"]
                elif position["bars"] >= max_hold:
                    exit_reason = "timeout"

            if exit_reason:
                if position["direction"] == "LONG":
                    pnl = (exit_price - position["entry"]) / position["entry"] * 100
                else:
                    pnl = (position["entry"] - exit_price) / position["entry"] * 100
                trades.append({
                    "pnl_pct": round(pnl, 3), "exit_reason": exit_reason,
                    "bars": position["bars"], "direction": position["direction"],
                })
                position = None

    return trades


# ═══════════════════════════════════════════════════════════════
# APOLLO — Mean Reversion
# ═══════════════════════════════════════════════════════════════

def apollo_backtest(symbol, ema_period=20, extension_atr_mult=2.0,
                    reversion_target_mult=0.5, stop_mult=0.5, max_hold=5):
    """Mean reversion: enter when price overextends from EMA, exit on snap-back."""
    df = load_daily(symbol)
    if len(df) < 60:
        return []

    df["ema"] = df["Close"].ewm(span=ema_period, adjust=False).mean()
    df["tr"] = np.maximum(
        df["High"] - df["Low"],
        np.maximum(abs(df["High"] - df["Close"].shift(1)), abs(df["Low"] - df["Close"].shift(1))),
    )
    df["atr"] = df["tr"].rolling(14).mean()
    df["dist_from_ema"] = df["Close"] - df["ema"]
    df["dist_atr"] = abs(df["dist_from_ema"]) / df["atr"]

    # RSI for confirmation
    delta = df["Close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss
    df["rsi"] = 100 - (100 / (1 + rs))

    trades = []
    position = None

    for i in range(max(ema_period, 20) + 5, len(df)):
        row = df.iloc[i]

        if position is None:
            overextended = row["dist_atr"] > extension_atr_mult

            if not overextended:
                continue

            # Short when overextended UP (RSI > 70)
            if row["dist_from_ema"] > 0 and row["rsi"] > 70:
                stop = row["Close"] + row["atr"] * stop_mult
                target = row["ema"] + row["atr"] * reversion_target_mult
                position = {
                    "direction": "SHORT", "entry": row["Close"],
                    "stop": stop, "target": target, "bars": 0,
                    "entry_date": df.index[i], "ema_at_entry": row["ema"],
                }
            # Long when overextended DOWN (RSI < 30)
            elif row["dist_from_ema"] < 0 and row["rsi"] < 30:
                stop = row["Close"] - row["atr"] * stop_mult
                target = row["ema"] - row["atr"] * reversion_target_mult
                position = {
                    "direction": "LONG", "entry": row["Close"],
                    "stop": stop, "target": target, "bars": 0,
                    "entry_date": df.index[i], "ema_at_entry": row["ema"],
                }
        else:
            position["bars"] += 1
            exit_reason = None
            exit_price = row["Close"]

            if position["direction"] == "LONG":
                if row["Low"] <= position["stop"]:
                    exit_reason = "stop"; exit_price = position["stop"]
                elif row["High"] >= position["target"]:
                    exit_reason = "target"; exit_price = position["target"]
                elif position["bars"] >= max_hold:
                    exit_reason = "timeout"
            else:
                if row["High"] >= position["stop"]:
                    exit_reason = "stop"; exit_price = position["stop"]
                elif row["Low"] <= position["target"]:
                    exit_reason = "target"; exit_price = position["target"]
                elif position["bars"] >= max_hold:
                    exit_reason = "timeout"

            if exit_reason:
                if position["direction"] == "LONG":
                    pnl = (exit_price - position["entry"]) / position["entry"] * 100
                else:
                    pnl = (position["entry"] - exit_price) / position["entry"] * 100
                trades.append({
                    "pnl_pct": round(pnl, 3), "exit_reason": exit_reason,
                    "bars": position["bars"], "direction": position["direction"],
                })
                position = None

    return trades


# ═══════════════════════════════════════════════════════════════
# ARTEMIS — Session Open Range Breakout
# ═══════════════════════════════════════════════════════════════

def artemis_backtest(symbol, range_bars=6, atr_stop_mult=1.0, max_hold=12):
    """Session open range breakout on hourly bars.
    First 6 hours establish range, trade the breakout.
    Uses hourly data — 6 bars = London morning range.
    """
    df = load_hourly(symbol)
    if df is None or len(df) < 100:
        return []

    df["tr"] = np.maximum(
        df["High"] - df["Low"],
        np.maximum(abs(df["High"] - df["Close"].shift(1)), abs(df["Low"] - df["Close"].shift(1))),
    )
    df["atr"] = df["tr"].rolling(24).mean()  # 24hr ATR on hourly

    # Group by trading day
    df["day"] = df.index.date
    trades = []

    for day, group in df.groupby("day"):
        if len(group) < range_bars + 4:
            continue

        # First N bars = open range
        open_range = group.iloc[:range_bars]
        range_high = open_range["High"].max()
        range_low = open_range["Low"].min()
        atr = group.iloc[range_bars]["atr"] if not pd.isna(group.iloc[range_bars]["atr"]) else None
        if atr is None or atr <= 0:
            continue

        position = None
        for i in range(range_bars, min(len(group), range_bars + max_hold)):
            row = group.iloc[i]

            if position is None:
                if row["Close"] > range_high:
                    stop = row["Close"] - atr * atr_stop_mult
                    position = {
                        "direction": "LONG", "entry": row["Close"],
                        "stop": stop, "bars": 0,
                    }
                elif row["Close"] < range_low:
                    stop = row["Close"] + atr * atr_stop_mult
                    position = {
                        "direction": "SHORT", "entry": row["Close"],
                        "stop": stop, "bars": 0,
                    }
            else:
                position["bars"] += 1
                exit_reason = None
                exit_price = row["Close"]

                if position["direction"] == "LONG":
                    if row["Low"] <= position["stop"]:
                        exit_reason = "stop"; exit_price = position["stop"]
                elif position["direction"] == "SHORT":
                    if row["High"] >= position["stop"]:
                        exit_reason = "stop"; exit_price = position["stop"]

                # End of session exit
                if i >= len(group) - 1 or position["bars"] >= max_hold:
                    exit_reason = exit_reason or "session_end"

                if exit_reason:
                    if position["direction"] == "LONG":
                        pnl = (exit_price - position["entry"]) / position["entry"] * 100
                    else:
                        pnl = (position["entry"] - exit_price) / position["entry"] * 100
                    trades.append({
                        "pnl_pct": round(pnl, 3), "exit_reason": exit_reason,
                        "bars": position["bars"], "direction": position["direction"],
                    })
                    position = None
                    break  # One trade per day max

    return trades


# ═══════════════════════════════════════════════════════════════
# MAIN — Run all strategies on all pairs
# ═══════════════════════════════════════════════════════════════

def main():
    fx_symbols = ["EURUSD", "GBPUSD", "USDJPY", "AUDJPY", "AUDUSD", "EURJPY", "GBPJPY", "CADJPY"]
    futures_symbols = ["GOLD_F", "NQ_F", "ES_F", "YM_F"]
    all_daily = fx_symbols + futures_symbols
    fx_hourly = [s for s in fx_symbols if (DATA_DIR / f"{s}_1h.csv").exists()]

    strategies = [
        ("HERMES (Momentum)", hermes_backtest, all_daily),
        ("APOLLO (Mean Reversion)", apollo_backtest, all_daily),
        ("ARTEMIS (Session ORB)", artemis_backtest, fx_hourly),
    ]

    for strat_name, strat_fn, symbols in strategies:
        print("=" * 95)
        print(f"  {strat_name}")
        print("=" * 95)
        print(f"{'Sym':>10s} | {'T':>4s} | {'WR':>5s} | {'PF':>6s} | {'Avg%':>7s} | {'Tot%':>8s} | {'DD%':>6s} | Exits")
        print("-" * 95)

        results = {}
        for sym in symbols:
            try:
                trades = strat_fn(sym)
            except Exception as e:
                print(f"{sym:>10s} | ERROR: {e}")
                continue

            m = compute_metrics(trades)
            if not m:
                print(f"{sym:>10s} | no trades")
                continue

            exit_str = " ".join(f"{k}={v}" for k, v in m["exits"].items())
            print(
                f"{sym:>10s} | {m['trades']:>4d} | {m['wr']:>4.0%} | {m['pf']:>5.2f} | "
                f"{m['avg']:>+6.3f}% | {m['total']:>+7.1f}% | {m['max_dd']:>5.1f}% | {exit_str}"
            )
            results[sym] = m

        if results:
            print()
            print(f"  Best: ", end="")
            best = max(results.items(), key=lambda x: x[1]["pf"])
            print(f"{best[0]} PF={best[1]['pf']}")
        print()


if __name__ == "__main__":
    main()
