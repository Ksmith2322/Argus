"""Helio POC Backtest — swing trading on daily bars.

Tests: trend following + range expansion + pullback to EMA + trailing stop.
Purpose: Prove or kill the swing thesis before building anything.
"""
import pandas as pd
import numpy as np
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"


def run_swing_backtest(symbol, ema_period=20, atr_period=14, atr_stop_mult=1.5,
                       atr_trail_mult=1.0, max_hold_days=5):
    df = pd.read_csv(DATA_DIR / f"{symbol}_daily.csv", parse_dates=["Date"])
    df = df.set_index("Date").sort_index()
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["ema20"] = df["Close"].ewm(span=ema_period, adjust=False).mean()
    df["ema_slope"] = df["ema20"].diff(3) / df["ema20"].shift(3)

    df["tr"] = np.maximum(
        df["High"] - df["Low"],
        np.maximum(abs(df["High"] - df["Close"].shift(1)), abs(df["Low"] - df["Close"].shift(1))),
    )
    df["atr"] = df["tr"].rolling(atr_period).mean()
    df["range_pct"] = (df["High"] - df["Low"]) / df["Close"]
    df["avg_range"] = df["range_pct"].rolling(20).mean()
    df["range_expanded"] = df["range_pct"] > df["avg_range"] * 1.2
    df["avg_vol"] = df["Volume"].rolling(20).mean()
    df["vol_surge"] = df["Volume"] > df["avg_vol"] * 1.2
    df["near_ema"] = abs(df["Close"] - df["ema20"]) < df["atr"] * 0.5

    trades = []
    position = None
    start = max(ema_period, atr_period) + 5

    for i in range(start, len(df)):
        row = df.iloc[i]

        if position is None:
            trend_up = row["Close"] > row["ema20"] and row["ema_slope"] > 0.001
            trend_down = row["Close"] < row["ema20"] and row["ema_slope"] < -0.001
            entry_a = row["range_expanded"] and row["vol_surge"]
            entry_b = row["near_ema"] and abs(row["ema_slope"]) > 0.002

            if trend_up and (entry_a or entry_b):
                stop = row["Close"] - row["atr"] * atr_stop_mult
                position = {
                    "direction": "LONG", "entry_price": row["Close"],
                    "entry_date": df.index[i], "stop": stop, "initial_stop": stop,
                    "trail_stop": stop, "highest": row["Close"], "bars_held": 0,
                    "entry_type": "A" if entry_a else "B",
                }
            elif trend_down and (entry_a or entry_b):
                stop = row["Close"] + row["atr"] * atr_stop_mult
                position = {
                    "direction": "SHORT", "entry_price": row["Close"],
                    "entry_date": df.index[i], "stop": stop, "initial_stop": stop,
                    "trail_stop": stop, "lowest": row["Close"], "bars_held": 0,
                    "entry_type": "A" if entry_a else "B",
                }
        else:
            position["bars_held"] += 1
            exit_reason = None
            exit_price = row["Close"]

            if position["direction"] == "LONG":
                position["highest"] = max(position["highest"], row["High"])
                risk = position["entry_price"] - position["initial_stop"]
                if risk > 0:
                    if position["highest"] - position["entry_price"] >= risk:
                        position["trail_stop"] = max(position["trail_stop"], position["entry_price"])
                    if position["highest"] - position["entry_price"] >= risk * 2:
                        new_trail = position["highest"] - row["atr"] * atr_trail_mult
                        position["trail_stop"] = max(position["trail_stop"], new_trail)
                position["stop"] = max(position["stop"], position["trail_stop"])
                if row["Low"] <= position["stop"]:
                    exit_reason = "stop" if position["stop"] == position["initial_stop"] else "trail"
                    exit_price = position["stop"]
                elif position["bars_held"] >= max_hold_days:
                    exit_reason = "timeout"
            else:
                position["lowest"] = min(position["lowest"], row["Low"])
                risk = position["initial_stop"] - position["entry_price"]
                if risk > 0:
                    if position["entry_price"] - position["lowest"] >= risk:
                        position["trail_stop"] = min(position["trail_stop"], position["entry_price"])
                    if position["entry_price"] - position["lowest"] >= risk * 2:
                        new_trail = position["lowest"] + row["atr"] * atr_trail_mult
                        position["trail_stop"] = min(position["trail_stop"], new_trail)
                position["stop"] = min(position["stop"], position["trail_stop"])
                if row["High"] >= position["stop"]:
                    exit_reason = "stop" if position["stop"] == position["initial_stop"] else "trail"
                    exit_price = position["stop"]
                elif position["bars_held"] >= max_hold_days:
                    exit_reason = "timeout"

            if exit_reason:
                if position["direction"] == "LONG":
                    pnl_pct = (exit_price - position["entry_price"]) / position["entry_price"] * 100
                else:
                    pnl_pct = (position["entry_price"] - exit_price) / position["entry_price"] * 100
                trades.append({
                    "entry_date": str(position["entry_date"])[:10],
                    "exit_date": str(df.index[i])[:10],
                    "direction": position["direction"],
                    "entry": round(position["entry_price"], 2),
                    "exit": round(exit_price, 2),
                    "pnl_pct": round(pnl_pct, 3),
                    "bars_held": position["bars_held"],
                    "exit_reason": exit_reason,
                    "entry_type": position["entry_type"],
                })
                position = None

    return trades


def main():
    symbols = ["SPY", "QQQ", "IWM", "DIA", "GLD", "TLT", "XLF", "SMH", "EEM"]

    print("=" * 90)
    print(f"  HELIO POC BACKTEST -- Swing Trading on Daily Bars (2yr)")
    print("=" * 90)
    print()
    print(f"{'Sym':>5s} | {'Trades':>6s} | {'WR':>5s} | {'PF':>6s} | {'Avg%':>7s} | {'Total%':>8s} | {'MaxDD%':>7s} | {'TO%':>5s} | {'Trail%':>6s} | {'Stop%':>5s}")
    print("-" * 90)

    all_results = {}
    for sym in symbols:
        try:
            trades = run_swing_backtest(sym)
        except Exception as e:
            print(f"{sym:>5s} | ERROR: {e}")
            continue

        if not trades:
            print(f"{sym:>5s} | {'0':>6s} | no trades")
            continue

        pnls = [t["pnl_pct"] for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        wr = len(wins) / len(pnls)
        gw = sum(wins)
        gl = abs(sum(losses))
        pf = gw / gl if gl > 0 else 99
        total = sum(pnls)
        avg = total / len(pnls)

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
        to_pct = exits.get("timeout", 0) / len(trades) * 100
        trail_pct = exits.get("trail", 0) / len(trades) * 100
        stop_pct = exits.get("stop", 0) / len(trades) * 100

        print(
            f"{sym:>5s} | {len(trades):>6d} | {wr:>4.0%} | {pf:>5.2f} | "
            f"{avg:>+6.2f}% | {total:>+7.1f}% | {max_dd:>6.1f}% | "
            f"{to_pct:>4.0f}% | {trail_pct:>5.0f}% | {stop_pct:>4.0f}%"
        )
        all_results[sym] = {"trades": len(trades), "pf": pf, "wr": wr, "total": total, "max_dd": max_dd}

    print()
    print("TOP CANDIDATES (PF > 1.3):")
    found = False
    for sym, r in sorted(all_results.items(), key=lambda x: -x[1]["pf"]):
        if r["pf"] > 1.3:
            print(f"  {sym}: PF={r['pf']:.2f} WR={r['wr']:.0%} Total={r['total']:+.1f}% Trades={r['trades']} MaxDD={r['max_dd']:.1f}%")
            found = True
    if not found:
        print("  None above 1.3 -- adjust parameters or strategy")

    print()
    print("ALL RESULTS:")
    for sym, r in sorted(all_results.items(), key=lambda x: -x[1]["pf"]):
        verdict = "STRONG" if r["pf"] > 1.5 else "VIABLE" if r["pf"] > 1.2 else "WEAK" if r["pf"] > 1.0 else "DEAD"
        print(f"  {sym}: PF={r['pf']:.2f} [{verdict}]")


if __name__ == "__main__":
    main()
