"""Sweep new instruments: Apollo on futures + Argus on new FX pairs."""
import sys, json, subprocess
sys.path.insert(0, "C:\\Argus\\repo")

import pandas as pd, numpy as np
from pathlib import Path
from helio.strategies_backtest import compute_metrics

DATA = Path("helio/data")


def apollo_on_tf(symbol, tf="1h", ema_period=20, extension_atr_mult=2.0,
                 stop_mult=0.5, max_hold=20):
    path = DATA / f"{symbol}_{tf}.csv"
    if not path.exists():
        return []
    df = pd.read_csv(path, parse_dates=["Date"]).set_index("Date").sort_index()
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna()
    if len(df) < 40:
        return []
    df["ema"] = df["Close"].ewm(span=ema_period, adjust=False).mean()
    df["tr"] = np.maximum(df["High"] - df["Low"],
                          np.maximum(abs(df["High"] - df["Close"].shift(1)),
                                     abs(df["Low"] - df["Close"].shift(1))))
    df["atr"] = df["tr"].rolling(14).mean()
    df["dist_from_ema"] = df["Close"] - df["ema"]
    df["dist_atr"] = abs(df["dist_from_ema"]) / df["atr"]
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
            if row["dist_atr"] > extension_atr_mult:
                if row["dist_from_ema"] > 0 and row["rsi"] > 70:
                    stop = row["Close"] + row["atr"] * stop_mult
                    target = row["ema"] + row["atr"] * 0.5
                    position = {"direction": "SHORT", "entry": row["Close"], "stop": stop, "target": target, "bars": 0}
                elif row["dist_from_ema"] < 0 and row["rsi"] < 30:
                    stop = row["Close"] - row["atr"] * stop_mult
                    target = row["ema"] - row["atr"] * 0.5
                    position = {"direction": "LONG", "entry": row["Close"], "stop": stop, "target": target, "bars": 0}
        else:
            position["bars"] += 1
            exit_reason = None
            exit_price = row["Close"]
            if position["direction"] == "LONG":
                if row["Low"] <= position["stop"]: exit_reason = "stop"; exit_price = position["stop"]
                elif row["High"] >= position["target"]: exit_reason = "target"; exit_price = position["target"]
                elif position["bars"] >= max_hold: exit_reason = "timeout"
            else:
                if row["High"] >= position["stop"]: exit_reason = "stop"; exit_price = position["stop"]
                elif row["Low"] <= position["target"]: exit_reason = "target"; exit_price = position["target"]
                elif position["bars"] >= max_hold: exit_reason = "timeout"
            if exit_reason:
                if position["direction"] == "LONG":
                    pnl = (exit_price - position["entry"]) / position["entry"] * 100
                else:
                    pnl = (position["entry"] - exit_price) / position["entry"] * 100
                trades.append({"pnl_pct": round(pnl, 3), "exit_reason": exit_reason,
                               "bars": position["bars"], "direction": position["direction"]})
                position = None
    return trades


if __name__ == "__main__":
    print("=== APOLLO ON FUTURES (1H) ===")
    for sym in ["MGC", "MNQ"]:
        trades = apollo_on_tf(sym, tf="1h")
        m = compute_metrics(trades)
        if m and m["trades"] >= 3:
            print(f"  {sym} 1H: T={m['trades']} WR={m['wr']:.0%} PF={m['pf']:.2f} Tot={m['total']:+.2f}% DD={m['max_dd']:.2f}%")
        else:
            print(f"  {sym} 1H: {m['trades'] if m else 0} trades")

    # Backtest Argus on new FX pairs (if data available)
    print("\n=== ARGUS ON NEW FX PAIRS ===")
    new_pairs = ["usdchf", "nzdusd", "eurgbp", "gbpaud", "euraud", "nzdjpy", "chfjpy", "audcad"]
    base_cfg = "argus_flow/configs/eurusd_t4_paper_v1.json"  # use as template

    for pair in new_pairs:
        data = Path(f"argus_flow/data/ibkr_{pair}_1m.csv")
        if not data.exists():
            print(f"  {pair.upper()}: no data yet")
            continue
        try:
            result = subprocess.run(
                [sys.executable, "-m", "argus_flow.ops.fx_backtest",
                 "--config", base_cfg, "--data", str(data)],
                capture_output=True, text=True, timeout=120, cwd="C:\\Argus\\repo"
            )
            for line in result.stdout.split("\n"):
                if line.strip().startswith("{"):
                    r = json.loads(line.strip())
                    print(f"  {pair.upper()}: T={r['trades']} WR={r['win_rate']:.0f}% PF={r['profit_factor']:.2f} Exp={r['expectancy']:+.1f} Total={r['total_pnl']:+.0f}")
                    break
            else:
                if result.stderr:
                    print(f"  {pair.upper()}: error")
                else:
                    print(f"  {pair.upper()}: no output")
        except Exception as e:
            print(f"  {pair.upper()}: {e}")
