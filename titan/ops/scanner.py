#!/usr/bin/env python3
"""titan/ops/scanner.py -- Nightly Titan swing signal scanner.

Evaluates all Titan universe instruments and posts trade signals to Discord.
Designed to run after market close (~4:30 PM ET / 21:30 UTC).

Usage:
    python -m titan.ops.scanner                  # Scan + post to Discord
    python -m titan.ops.scanner --dry-run        # Print only
    python -m titan.ops.scanner --refresh        # Re-download data first
    python -m titan.ops.scanner --long-only      # Only show long signals
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv
load_dotenv(REPO / ".env")

from titan.ops.data_pipeline import UNIVERSE, download_universe, get_data
from titan.strategies.swing_engine import SwingEngine
from titan.strategies.ai_overlay import SwingOverlay, SwingMarketState

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
LOGS_DIR = REPO / "titan" / "logs"

# Focus on proven winners from backtest
FOCUS_SYMBOLS = ["GLD", "GDX", "PLTR", "MRNA", "QQQ", "SPY", "SLV", "TSLA", "MARA", "USO"]


def scan_signals(symbols: list[str] | None = None,
                 long_only: bool = False,
                 min_strength: int = 60) -> list[dict]:
    """Evaluate all symbols and return active signals with AI overlay scoring."""
    engine = SwingEngine({"min_signal_strength": min_strength})
    overlay = SwingOverlay(symbol="FLEET", state_dir=LOGS_DIR)
    syms = symbols or FOCUS_SYMBOLS
    signals = []

    for sym in syms:
        daily = get_data(sym, "daily")
        h4 = get_data(sym, "4h")

        if daily is None or len(daily) < 60:
            continue

        signal = engine.evaluate(sym, daily, h4)
        if signal is None:
            continue

        if long_only and signal.direction == "short":
            continue

        # AI overlay evaluation
        close = daily["Close"].values
        volume = daily["Volume"].values if "Volume" in daily.columns else [0]
        vol_20 = pd.Series(volume).rolling(20).mean().values
        ema_50 = pd.Series(close).ewm(span=50).mean().values
        ema_200 = pd.Series(close).ewm(span=200).mean().values
        i = len(close) - 1
        vol_ratio = volume[i] / vol_20[i] if vol_20[i] > 0 and not np.isnan(vol_20[i]) else 1.0
        dist_ema50 = (close[i] - ema_50[i]) / ema_50[i] * 100 if ema_50[i] > 0 else 0
        ret_5d = (close[i] / close[max(0, i - 5)] - 1) * 100 if i >= 5 else 0
        ret_20d = (close[i] / close[max(0, i - 20)] - 1) * 100 if i >= 20 else 0

        ai_state = SwingMarketState(
            price=signal.entry_price,
            atr_pct=signal.atr_pct,
            rsi_daily=signal.rsi_daily,
            volume_ratio=vol_ratio,
            bb_pctile=signal.bb_pctile,
            dist_from_ema50_pct=dist_ema50,
            ema_aligned=signal.daily_trend == "UP",
            above_ema200=close[i] > ema_200[i] if i < len(ema_200) else False,
            adx=0,  # ADX computed inside engine, use signal context
            day_of_week=datetime.now().weekday(),
            signal_strength=signal.strength,
            strategy_type=signal.strategy,
            sector=UNIVERSE.get(sym, {}).get("sector", "") if isinstance(UNIVERSE.get(sym), dict) else UNIVERSE.get(sym, ""),
            return_5d=ret_5d,
            return_20d=ret_20d,
        )
        ai_decision = overlay.evaluate(signal.direction, ai_state)

        signals.append({
            "symbol": signal.symbol,
            "direction": signal.direction.upper(),
            "strength": signal.strength,
            "strategy": signal.strategy,
            "entry": signal.entry_price,
            "stop": signal.stop_price,
            "target": signal.target_price,
            "rr": signal.risk_reward,
            "daily_trend": signal.daily_trend,
            "rsi": signal.rsi_daily,
            "atr_pct": signal.atr_pct,
            "volume": vol_ratio,
            "reason": signal.reason,
            "ai_action": ai_decision.action,
            "ai_consensus": ai_decision.consensus_score,
            "ai_for": ai_decision.voters_for,
            "ai_against": ai_decision.voters_against,
            "ai_reason": ai_decision.reason,
        })

    signals.sort(key=lambda s: s["strength"], reverse=True)
    return signals


def format_discord(signals: list[dict]) -> str:
    """Format signals for Discord."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"**TITAN Swing Scanner -- {now}**\n"]

    if not signals:
        lines.append("No active swing signals today. Standing aside.")
        return "\n".join(lines)

    hot = [s for s in signals if s["strength"] >= 70]
    warm = [s for s in signals if 60 <= s["strength"] < 70]

    if hot:
        lines.append(f"**ACTIVE SIGNALS ({len(hot)}):**")
        for s in hot:
            risk_pct = abs(s["entry"] - s["stop"]) / s["entry"] * 100
            ai_tag = f"AI:{s.get('ai_action','?')}({s.get('ai_consensus',0):+.2f})"
            lines.append(
                f"  **{s['symbol']}** {s['direction']} [{s['strategy']}] "
                f"score={s['strength']} {ai_tag}\n"
                f"    Entry: ${s['entry']} | Stop: ${s['stop']} ({risk_pct:.1f}% risk) | "
                f"Target: ${s['target']} | R:R {s['rr']}\n"
                f"    RSI={s['rsi']:.0f} | ATR={s['atr_pct']:.1f}% | {s['reason']}"
            )

    if warm:
        lines.append(f"\n**WATCHLIST ({len(warm)}):**")
        for s in warm:
            lines.append(
                f"  {s['symbol']} {s['direction']} [{s['strategy']}] "
                f"score={s['strength']} | ${s['entry']} | {s['reason'][:60]}"
            )

    return "\n".join(lines)


def send_discord(content: str) -> bool:
    if not WEBHOOK_URL:
        return False
    import requests
    try:
        chunks = [content[i:i + 1900] for i in range(0, len(content), 1900)]
        ok = True
        for chunk in chunks:
            r = requests.post(WEBHOOK_URL, json={"content": chunk}, timeout=10)
            ok = ok and r.status_code in (200, 204)
            time.sleep(0.5)
        return ok
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(description="Titan Nightly Scanner")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--refresh", action="store_true", help="Re-download data")
    parser.add_argument("--long-only", action="store_true", help="Long signals only")
    parser.add_argument("--min-strength", type=int, default=60)
    parser.add_argument("--symbols", nargs="+")
    args = parser.parse_args()

    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    if args.refresh:
        print("Refreshing data...")
        download_universe(symbols=args.symbols or FOCUS_SYMBOLS, timeframes=["daily", "4h"], force=True)

    print(f"{'=' * 60}")
    print(f"TITAN Scanner -- {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'=' * 60}")

    signals = scan_signals(
        symbols=args.symbols,
        long_only=args.long_only,
        min_strength=args.min_strength,
    )

    # Print
    print(f"\n{'Symbol':8s} {'Dir':6s} {'Str':>4s} {'Strategy':18s} {'Entry':>8s} "
          f"{'Stop':>8s} {'Target':>8s} {'R:R':>5s} {'RSI':>5s}")
    print("-" * 80)
    for s in signals:
        print(f"{s['symbol']:8s} {s['direction']:6s} {s['strength']:4d} {s['strategy']:18s} "
              f"${s['entry']:>7.2f} ${s['stop']:>7.2f} ${s['target']:>7.2f} "
              f"{s['rr']:5.2f} {s['rsi']:5.1f}")

    if not signals:
        print("  No signals.")

    # Discord
    report = format_discord(signals)
    print(f"\n{report}")

    if not args.dry_run:
        ok = send_discord(report)
        print(f"\nDiscord: {'sent' if ok else 'FAILED'}")

    # Save
    log_path = LOGS_DIR / f"scan_{datetime.now().strftime('%Y%m%d')}.json"
    log_path.write_text(json.dumps(signals, indent=2, default=str))
    print(f"Saved: {log_path}")


if __name__ == "__main__":
    main()
