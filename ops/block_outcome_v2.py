"""ops/block_outcome_v2 — price-joined counterfactual P&L for blocked signals.

Extends the MVP `block_outcome_tracker` with realized-price data:

  For each blocked signal at time T on strategy S, compute what would have
  happened directionally if the trade had been taken. Heuristic:

    1. Map S → (underlying_symbol, expected_direction).
    2. Read daily close at T_day and at T_day + 1 trading day (yfinance).
    3. realized_move_pct = (close[T+1] - close[T]) / close[T] * 100
    4. counterfactual_pct = realized_move_pct  if direction = LONG
                          = -realized_move_pct if direction = SHORT
    5. save = (counterfactual_pct < 0)   ← block kept us out of a losing trade
       miss = (counterfactual_pct > 0)   ← block kept us out of a winning trade

  Aggregate per (strategy × reason): n, mean_counterfactual_pct, save_rate.

  Only reasons with n ≥ MIN_SAMPLES_PER_REASON are emitted — small samples
  produce noise that swings ±5% on one outlier.

Limits (be honest about these):
  • Daily resolution. A signal at 10:00am gets matched to that day's close,
    smearing intraday entries that would have stopped out by 1pm.
  • Strategy direction is FIXED per strategy. Strategies that go both ways
    (mean-reverters that buy oversold + fade overbought) are oversimplified.
  • 30-day window. Most reasons won't have n ≥ 10 yet. As fleet runs longer,
    signal density grows.

Output: argus_flow/logs/block_outcomes_v2_latest.json

Usage:
    cd c:/Argus/repo
    C:/Argus/.venv/Scripts/python.exe -m ops.block_outcome_v2

Schedule: daily (yfinance is slow + rate-limited; hourly would burn it).
"""
from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Match the MVP tracker: strip trailing `_<num>` so `rsi_neutral_67.6` and
# `rsi_neutral_20.0` aggregate as one reason instead of splitting into 50.
_NUMERIC_SUFFIX_RE = re.compile(r"(_\d+(\.\d+)?)+$")


def _normalize_reason(raw: str) -> str:
    return _NUMERIC_SUFFIX_RE.sub("", (raw or "unknown").lower()) or "unknown"

REPO = Path(__file__).resolve().parents[1]
OUT_PATH = REPO / "argus_flow" / "logs" / "block_outcomes_v2_latest.json"

WINDOW_DAYS = 30
MIN_SAMPLES_PER_REASON = 10
HOLD_DAYS = 1  # how far ahead to read price

# Strategy → (yfinance_symbol, expected_direction).
# Direction is HEURISTIC and reflects the strategy's dominant bias. None
# means "mixed long/short" — those strategies fall back to absolute-move
# saved-rate (where save = small move, miss = big move).
SYMBOL_MAP: dict[str, tuple[str, str | None]] = {
    "multi_orb":          ("QQQ",       "LONG"),    # opening-range breakout (post 4/30 QQQ-only)
    "spy_mean_rev":       ("SPY",       "LONG"),    # buys oversold RSI(2)
    "vix_intraday":       ("UVXY",      "LONG"),    # long vol when VIX oversold
    "nq_overnight":       ("QQQ",       "LONG"),    # overnight long bias
    "nq_london_close":    ("QQQ",       "SHORT"),   # afternoon fade
    "gld_pm_long":        ("GLD",       "LONG"),
    "jpy_pm_short":       ("JPY=X",     "SHORT"),   # short USDJPY in PM (historically)
    "aud_asian_breakout": ("AUDUSD=X",  "LONG"),
    "wick_gbpusd":        ("GBPUSD=X",  None),      # wick reversal — both directions
    "fomc_drift":         ("SPY",       None),      # event-driven, mixed direction
    "mamba":              ("QQQ",       None),      # multi-bias
}


def _parse_ts(raw: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(raw or "").replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _classify_action(action: str) -> tuple[str, str | None]:
    a = (action or "").upper().strip()
    if a.startswith("ENTRY_") or a.startswith("ENTER_"):
        return ("ENTRY", None)
    if a.startswith("NO_TRIGGER_"):
        return ("BLOCKED", _normalize_reason(a[len("NO_TRIGGER_"):]))
    return ("OTHER", None)


def _fetch_daily_closes(symbols: list[str], window_days: int) -> dict[str, dict[str, float]]:
    """Return {symbol: {YYYY-MM-DD: close}} via yfinance. Empty on failure."""
    try:
        import yfinance as yf  # type: ignore
        import pandas as pd  # type: ignore
    except ImportError:
        return {}
    out: dict[str, dict[str, float]] = {}
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=window_days + 5)
    for sym in symbols:
        try:
            df = yf.download(
                sym, start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(),
                progress=False, auto_adjust=True,
            )
            if df is None or df.empty:
                continue
            close = df["Close"]
            if hasattr(close, "ndim") and close.ndim == 2:
                close = close.iloc[:, 0]
            out[sym] = {idx.strftime("%Y-%m-%d"): float(v) for idx, v in close.items()}
        except Exception:
            continue
    return out


def _next_trading_close(closes: dict[str, float], date_str: str, hold_days: int) -> tuple[str, float] | None:
    """Find the close `hold_days` trading days after `date_str`. Returns (date, close) or None."""
    sorted_dates = sorted(closes.keys())
    try:
        # Find first date >= date_str, then advance hold_days
        idx = next(i for i, d in enumerate(sorted_dates) if d >= date_str)
    except StopIteration:
        return None
    target_idx = idx + hold_days
    if target_idx >= len(sorted_dates):
        return None
    target_date = sorted_dates[target_idx]
    return (target_date, closes[target_date])


def main() -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)

    # Phase 1: collect blocked signals per (strategy, reason) with their dates
    by_key: dict[tuple[str, str], list[str]] = defaultdict(list)  # date strings
    for csv_path in sorted((REPO / "forge" / "logs").glob("*/signals.csv")):
        strat = csv_path.parent.name
        if strat not in SYMBOL_MAP:
            continue
        try:
            with csv_path.open(encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    ts_raw = row.get("ts") or row.get("timestamp") or ""
                    ts = _parse_ts(ts_raw)
                    if not ts or ts < cutoff:
                        continue
                    kind, reason = _classify_action(row.get("action") or "")
                    if kind != "BLOCKED":
                        continue
                    by_key[(strat, reason or "unknown")].append(ts.strftime("%Y-%m-%d"))
        except Exception:
            continue

    if not by_key:
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(
            json.dumps({"evaluated_at_utc": datetime.now(timezone.utc).isoformat(),
                        "results": [], "method_note": "no blocked signals in window"}, indent=2),
            encoding="utf-8",
        )
        print("No blocked signals found.")
        return 0

    # Phase 2: fetch daily closes for all referenced symbols (one batch)
    needed_symbols = sorted({SYMBOL_MAP[s][0] for (s, _) in by_key.keys() if s in SYMBOL_MAP})
    print(f"Fetching daily closes for {len(needed_symbols)} symbols: {needed_symbols}")
    closes_by_sym = _fetch_daily_closes(needed_symbols, WINDOW_DAYS)
    print(f"Got data for {len(closes_by_sym)} symbols")

    # Phase 3: compute counterfactual per (strategy, reason)
    results: list[dict] = []
    for (strat, reason), dates in sorted(by_key.items()):
        sym, direction = SYMBOL_MAP[strat]
        closes = closes_by_sym.get(sym, {})
        if not closes:
            continue

        n = 0
        cf_values: list[float] = []
        for d in dates:
            entry = _next_trading_close(closes, d, 0)
            exit_ = _next_trading_close(closes, d, HOLD_DAYS)
            if not entry or not exit_:
                continue
            ec = entry[1]
            xc = exit_[1]
            if ec <= 0:
                continue
            move_pct = (xc - ec) / ec * 100.0
            if direction == "LONG":
                cf = move_pct
            elif direction == "SHORT":
                cf = -move_pct
            else:
                # Mixed direction — use absolute magnitude as a proxy for "missed move"
                cf = abs(move_pct)
            cf_values.append(cf)
            n += 1

        if n < MIN_SAMPLES_PER_REASON:
            continue

        mean_cf = sum(cf_values) / n
        # save = block kept us out of a losing trade (counterfactual < 0)
        # Only meaningful when direction is known
        if direction in ("LONG", "SHORT"):
            saves = sum(1 for v in cf_values if v < 0)
            save_rate = saves / n * 100.0
        else:
            save_rate = None

        results.append({
            "strategy": strat,
            "reason": reason,
            "symbol": sym,
            "direction": direction or "MIXED",
            "n_samples": n,
            "mean_counterfactual_pct": round(mean_cf, 3),
            "save_rate_pct": round(save_rate, 1) if save_rate is not None else None,
            "verdict": _verdict(mean_cf, save_rate, direction),
        })

    results.sort(key=lambda r: -r["n_samples"])

    payload = {
        "evaluated_at_utc": datetime.now(timezone.utc).isoformat(),
        "window_days": WINDOW_DAYS,
        "hold_days": HOLD_DAYS,
        "min_samples_per_reason": MIN_SAMPLES_PER_REASON,
        "results": results,
        "method_note": (
            "Daily-resolution counterfactual. Per (strategy, reason): mean P&L "
            "of trades the gate blocked, assuming you'd have entered at that "
            "day's close and exited 1 trading day later. save_rate is % of "
            "blocks that protected from negative counterfactual P&L (only "
            "meaningful for LONG/SHORT-biased strategies; MIXED uses absolute "
            "move). Direction map is in SYMBOL_MAP — heuristic per-strategy bias."
        ),
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print()
    print("=" * 90)
    print(f"  BLOCK OUTCOME V2 — {WINDOW_DAYS}d window, {HOLD_DAYS}d hold")
    print("=" * 90)
    if not results:
        print(f"  No (strategy, reason) pairs reached min_samples={MIN_SAMPLES_PER_REASON}")
    else:
        print(f"  {'Strategy':22s}  {'Reason':22s}  {'Sym':6s}  {'Dir':6s}  {'n':>4s}  {'mean_cf%':>9s}  {'save%':>6s}  verdict")
        print("  " + "-" * 88)
        for r in results[:30]:
            sr = f"{r['save_rate_pct']:.0f}" if r['save_rate_pct'] is not None else "—"
            print(
                f"  {r['strategy'][:22]:22s}  "
                f"{r['reason'][:22]:22s}  "
                f"{r['symbol']:6s}  "
                f"{r['direction']:6s}  "
                f"{r['n_samples']:>4}  "
                f"{r['mean_counterfactual_pct']:>+8.2f}%  "
                f"{sr:>5}%  "
                f"{r['verdict']}"
            )
    print()
    print(f"  Output: {OUT_PATH.relative_to(REPO)}")
    return 0


def _verdict(mean_cf: float, save_rate: float | None, direction: str | None) -> str:
    """Heuristic verdict on whether the gate is saving or costing alpha."""
    if direction not in ("LONG", "SHORT") or save_rate is None:
        if abs(mean_cf) < 0.5:
            return "QUIET (small avg moves)"
        return "MIXED (mixed-direction strategy — save/miss not classifiable)"
    if save_rate >= 60 and mean_cf < -0.2:
        return "SAVING ALPHA (gate avoids losing trades)"
    if save_rate <= 40 and mean_cf > 0.2:
        return "COSTING ALPHA (gate skips winners)"
    if save_rate >= 50:
        return "PROTECTIVE (more saves than misses)"
    return "NEUTRAL (no clear edge either way)"


if __name__ == "__main__":
    raise SystemExit(main())
