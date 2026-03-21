#!/usr/bin/env python3
"""
Analyze missed MISSED_BUY_WATCH_GATED signals — what would have happened?

Reads live_events.csv for ETH and BTC, finds near-miss signals (smallest gap
between conf_score and min threshold), then looks forward in time to compute
MFE/MAE for each signal to determine whether the missed entries would have
been profitable.

Usage:
    python analyze_missed_mfe_mae.py [--hours 72] [--top 50] [--forward-minutes 60]
"""

import argparse
import csv
import io
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
LOGS_ROOT = Path(r"C:\Argus\repo\ops\logs")
COIN_DIRS = {
    "ETH": LOGS_ROOT / "eth" / "live_events.csv",
    "BTC": LOGS_ROOT / "btc" / "live_events.csv",
}


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------
def parse_ts(ts_str: str) -> Optional[datetime]:
    """Parse ISO-8601 timestamp with timezone."""
    ts_str = ts_str.strip()
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str)
    except Exception:
        return None


def extract_from_detail(detail: str, key: str) -> Optional[str]:
    """Extract value for a key=value pair from a pipe-separated detail string."""
    # Try key=value pattern (space or pipe delimited)
    pattern = rf"(?:^|[\s|]){re.escape(key)}=([^\s|]+)"
    m = re.search(pattern, detail)
    if m:
        return m.group(1)
    return None


def extract_penalties(detail: str) -> dict:
    """Extract all penalty-related components from the detail field."""
    penalties = {}

    # liq_penalty
    val = extract_from_detail(detail, "liq_penalty")
    if val:
        try:
            penalties["liq_penalty"] = float(val)
        except ValueError:
            pass

    # session_bonus
    m = re.search(r"session_bonus=([+-]?\d+)", detail)
    if m:
        penalties["session_bonus"] = int(m.group(1))

    # base_score
    val = extract_from_detail(detail, "base_score")
    if val:
        try:
            penalties["base_score"] = int(val)
        except ValueError:
            pass

    # delta (governor adjustment)
    val = extract_from_detail(detail, "delta")
    if val:
        try:
            penalties["delta"] = int(val)
        except ValueError:
            pass

    # adj_score (after all adjustments, before final conf_score)
    val = extract_from_detail(detail, "adj_score")
    if val:
        try:
            penalties["adj_score"] = int(val)
        except ValueError:
            pass

    # trend_strength
    val = extract_from_detail(detail, "trend_strength")
    if val:
        try:
            penalties["trend_strength"] = float(val)
        except ValueError:
            pass

    # vol
    val = extract_from_detail(detail, "vol")
    if val and not val.startswith("1m"):
        try:
            penalties["vol"] = float(val)
        except ValueError:
            pass

    # conf_reasons
    m = re.search(r"conf_reasons=([^|]+)", detail)
    if m:
        penalties["conf_reasons"] = m.group(1).strip()

    # violations
    m = re.search(r"violations=([^;|]+)", detail)
    if m:
        penalties["violations"] = m.group(1).strip()

    return penalties


def parse_missed_event(row: dict) -> Optional[dict]:
    """Parse a MISSED_BUY_WATCH_GATED row into a structured dict."""
    detail = row.get("detail", "")
    if not detail:
        return None

    ts = parse_ts(row.get("ts", ""))
    if ts is None:
        return None

    px_str = extract_from_detail(detail, "px")
    conf_str = extract_from_detail(detail, "conf_score")
    min_str = extract_from_detail(detail, "min")
    regime = extract_from_detail(detail, "regime")

    if not all([px_str, conf_str, min_str]):
        return None

    try:
        px = float(px_str)
        conf_score = int(conf_str)
        min_score = int(min_str)
    except (ValueError, TypeError):
        return None

    gap = min_score - conf_score  # how far below threshold

    penalties = extract_penalties(detail)
    has_liq_penalty = "liq_penalty" in penalties and penalties["liq_penalty"] < 0

    return {
        "ts": ts,
        "symbol": row.get("symbol", ""),
        "px": px,
        "conf_score": conf_score,
        "min_score": min_score,
        "gap": gap,
        "regime": regime or "UNKNOWN",
        "penalties": penalties,
        "has_liq_penalty": has_liq_penalty,
        "detail_raw": detail[:200],  # truncated for display
    }


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def load_events(csv_path: Path, cutoff_ts: datetime) -> list[dict]:
    """Load all events from a live_events.csv, filtered by cutoff timestamp."""
    events = []
    if not csv_path.exists():
        print(f"  WARNING: {csv_path} not found, skipping")
        return events

    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = parse_ts(row.get("ts", ""))
            if ts is None:
                continue
            if ts < cutoff_ts:
                continue
            events.append(row)

    return events


def extract_price_series(events: list[dict]) -> list[tuple[datetime, float]]:
    """Extract (timestamp, price) pairs from events that have px= in detail."""
    series = []
    for row in events:
        ts = parse_ts(row.get("ts", ""))
        if ts is None:
            continue
        detail = row.get("detail", "")
        px_str = extract_from_detail(detail, "px")
        if px_str:
            try:
                series.append((ts, float(px_str)))
            except ValueError:
                continue
    return series


# ---------------------------------------------------------------------------
# Episode grouping
# ---------------------------------------------------------------------------
def group_into_episodes(signals: list[dict], gap_seconds: float = 30.0) -> list[dict]:
    """
    Group consecutive near-miss signals within gap_seconds of each other.
    Returns one representative per episode (the one with the smallest gap).
    """
    if not signals:
        return []

    # Sort by timestamp
    signals_sorted = sorted(signals, key=lambda s: s["ts"])

    episodes = []
    current_group = [signals_sorted[0]]

    for sig in signals_sorted[1:]:
        prev = current_group[-1]
        dt = (sig["ts"] - prev["ts"]).total_seconds()
        if dt <= gap_seconds:
            current_group.append(sig)
        else:
            # Pick representative: smallest gap (closest to trigger)
            best = min(current_group, key=lambda s: s["gap"])
            best["episode_size"] = len(current_group)
            best["episode_start"] = current_group[0]["ts"]
            best["episode_end"] = current_group[-1]["ts"]
            episodes.append(best)
            current_group = [sig]

    # Last group
    best = min(current_group, key=lambda s: s["gap"])
    best["episode_size"] = len(current_group)
    best["episode_start"] = current_group[0]["ts"]
    best["episode_end"] = current_group[-1]["ts"]
    episodes.append(best)

    return episodes


# ---------------------------------------------------------------------------
# Forward MFE/MAE computation
# ---------------------------------------------------------------------------
def compute_forward_mfe_mae(
    entry_ts: datetime,
    entry_px: float,
    price_series: list[tuple[datetime, float]],
    forward_seconds: float = 3600.0,
) -> dict:
    """
    Given an entry timestamp and price, look at future prices to compute MFE/MAE.
    For a BUY signal: MFE = max price - entry, MAE = entry - min price.
    """
    window_end = entry_ts + timedelta(seconds=forward_seconds)

    # Collect prices in the forward window (strictly after entry)
    future_prices = []
    for ts, px in price_series:
        if ts <= entry_ts:
            continue
        if ts > window_end:
            break
        future_prices.append((ts, px))

    if not future_prices:
        return {
            "mfe": 0.0,
            "mae": 0.0,
            "mfe_pct": 0.0,
            "mae_pct": 0.0,
            "time_to_mfe_s": 0.0,
            "n_prices": 0,
            "winner": False,
            "tp_hit": False,
            "sl_hit": False,
            "tp_sl_result": "NO_DATA",
            "final_px": entry_px,
            "final_pnl_pct": 0.0,
        }

    max_px = max(p[1] for p in future_prices)
    min_px = min(p[1] for p in future_prices)
    final_px = future_prices[-1][1]

    mfe = max_px - entry_px
    mae = entry_px - min_px

    mfe_pct = (mfe / entry_px) * 100 if entry_px > 0 else 0.0
    mae_pct = (mae / entry_px) * 100 if entry_px > 0 else 0.0

    # Time to MFE
    time_to_mfe_s = 0.0
    for ts, px in future_prices:
        if px == max_px:
            time_to_mfe_s = (ts - entry_ts).total_seconds()
            break

    winner = mfe > mae

    # Bucket analysis: 1% TP / 0.5% SL
    tp_threshold = entry_px * 1.01  # 1% above
    sl_threshold = entry_px * 0.995  # 0.5% below
    tp_hit = False
    sl_hit = False
    tp_sl_result = "OPEN"  # neither hit in window

    for ts, px in future_prices:
        if not tp_hit and px >= tp_threshold:
            tp_hit = True
            if not sl_hit:
                tp_sl_result = "TP_WIN"
                break
        if not sl_hit and px <= sl_threshold:
            sl_hit = True
            if not tp_hit:
                tp_sl_result = "SL_LOSS"
                break

    if tp_hit and sl_hit and tp_sl_result == "OPEN":
        # Both hit — we already broke on whichever came first
        pass

    final_pnl_pct = ((final_px - entry_px) / entry_px) * 100 if entry_px > 0 else 0.0

    return {
        "mfe": mfe,
        "mae": mae,
        "mfe_pct": mfe_pct,
        "mae_pct": mae_pct,
        "time_to_mfe_s": time_to_mfe_s,
        "n_prices": len(future_prices),
        "winner": winner,
        "tp_hit": tp_hit,
        "sl_hit": sl_hit,
        "tp_sl_result": tp_sl_result,
        "final_px": final_px,
        "final_pnl_pct": final_pnl_pct,
    }


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------
def fmt_pct(val: float) -> str:
    return f"{val:+.4f}%"


def fmt_price(val: float) -> str:
    if val > 1000:
        return f"${val:,.2f}"
    return f"${val:.3f}"


def print_separator(char: str = "=", width: int = 120):
    print(char * width)


def print_summary_stats(label: str, results: list[dict]):
    """Print aggregate stats for a group of results."""
    if not results:
        print(f"\n  {label}: No signals")
        return

    n = len(results)
    winners = [r for r in results if r["winner"]]
    tp_wins = [r for r in results if r["tp_sl_result"] == "TP_WIN"]
    sl_losses = [r for r in results if r["tp_sl_result"] == "SL_LOSS"]
    open_results = [r for r in results if r["tp_sl_result"] == "OPEN"]
    no_data = [r for r in results if r["tp_sl_result"] == "NO_DATA"]

    avg_mfe = sum(r["mfe_pct"] for r in results) / n
    avg_mae = sum(r["mae_pct"] for r in results) / n
    avg_final = sum(r["final_pnl_pct"] for r in results) / n
    median_mfe = sorted(r["mfe_pct"] for r in results)[n // 2]
    median_mae = sorted(r["mae_pct"] for r in results)[n // 2]
    avg_time_mfe = sum(r["time_to_mfe_s"] for r in results) / n

    valid = [r for r in results if r["n_prices"] > 0]
    n_valid = len(valid)

    print(f"\n  {label}  ({n} episodes, {n_valid} with forward price data)")
    print(f"    MFE > MAE ('winners'):  {len(winners)}/{n_valid} = {len(winners)/n_valid*100:.1f}%" if n_valid else "    No valid data")
    print(f"    Avg MFE%:               {fmt_pct(avg_mfe)}")
    print(f"    Avg MAE%:               {fmt_pct(avg_mae)}")
    print(f"    Median MFE%:            {fmt_pct(median_mfe)}")
    print(f"    Median MAE%:            {fmt_pct(median_mae)}")
    print(f"    Avg final PnL%:         {fmt_pct(avg_final)}")
    print(f"    Avg time to MFE:        {avg_time_mfe:.0f}s ({avg_time_mfe/60:.1f}min)")
    print(f"    --- 1% TP / 0.5% SL bucket ---")
    print(f"    TP wins:                {len(tp_wins)}/{n_valid} = {len(tp_wins)/n_valid*100:.1f}%" if n_valid else "")
    print(f"    SL losses:              {len(sl_losses)}/{n_valid} = {len(sl_losses)/n_valid*100:.1f}%" if n_valid else "")
    print(f"    Neither (still open):   {len(open_results)}/{n_valid} = {len(open_results)/n_valid*100:.1f}%" if n_valid else "")
    if no_data:
        print(f"    No forward data:        {len(no_data)}")

    if tp_wins or sl_losses:
        tp_count = len(tp_wins)
        sl_count = len(sl_losses)
        # Net expectancy per trade with 1% TP / 0.5% SL
        net = (tp_count * 1.0 - sl_count * 0.5) / (tp_count + sl_count) if (tp_count + sl_count) > 0 else 0
        print(f"    TP/SL expectancy:       {fmt_pct(net)} per trade")
        print(f"    TP/SL win rate:         {tp_count/(tp_count+sl_count)*100:.1f}%")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Analyze missed near-miss signals: what would MFE/MAE have been?"
    )
    parser.add_argument("--hours", type=int, default=72, help="How far back to look (hours)")
    parser.add_argument("--top", type=int, default=50, help="Top N near-miss episodes to analyze")
    parser.add_argument("--forward-minutes", type=int, default=60, help="Forward window for MFE/MAE (minutes)")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=args.hours)
    forward_seconds = args.forward_minutes * 60

    print_separator()
    print(f"  MISSED SIGNAL MFE/MAE ANALYSIS")
    print(f"  Window: last {args.hours}h  (cutoff: {cutoff.isoformat()})")
    print(f"  Forward window: {args.forward_minutes}min ({forward_seconds}s)")
    print(f"  Top N episodes: {args.top}")
    print_separator()

    all_episodes = []
    coin_episodes = {}

    for coin, csv_path in COIN_DIRS.items():
        print(f"\n--- Loading {coin} from {csv_path} ---")

        events = load_events(csv_path, cutoff)
        if not events:
            print(f"  No events found for {coin} after cutoff")
            continue

        print(f"  Total events loaded: {len(events)}")

        # Extract price series (from ALL events with px=)
        price_series = extract_price_series(events)
        print(f"  Price observations: {len(price_series)}")

        # Find MISSED_BUY_WATCH_GATED events
        missed_signals = []
        for row in events:
            if row.get("event", "").strip() != "MISSED_BUY_WATCH_GATED":
                continue
            parsed = parse_missed_event(row)
            if parsed:
                parsed["coin"] = coin
                missed_signals.append(parsed)

        print(f"  MISSED_BUY_WATCH_GATED signals: {len(missed_signals)}")

        if not missed_signals:
            continue

        # Group into episodes
        episodes = group_into_episodes(missed_signals, gap_seconds=30.0)
        print(f"  Unique episodes (30s grouping): {len(episodes)}")

        # Sort by gap (smallest first = closest to trigger)
        episodes.sort(key=lambda e: e["gap"])

        # Compute MFE/MAE for each episode
        for ep in episodes:
            result = compute_forward_mfe_mae(
                ep["ts"], ep["px"], price_series, forward_seconds
            )
            ep.update(result)

        coin_episodes[coin] = episodes
        all_episodes.extend(episodes)

    if not all_episodes:
        print("\nNo MISSED_BUY_WATCH_GATED episodes found. Nothing to analyze.")
        return

    # Sort all episodes by gap (closest to trigger first)
    all_episodes.sort(key=lambda e: (e["gap"], -e["conf_score"]))

    # Take top N
    top_episodes = all_episodes[: args.top]

    # -----------------------------------------------------------------------
    # Per-signal detail
    # -----------------------------------------------------------------------
    print_separator()
    print(f"  TOP {len(top_episodes)} NEAR-MISS EPISODES (sorted by gap to threshold)")
    print_separator()

    for i, ep in enumerate(top_episodes, 1):
        w_label = "WINNER" if ep["winner"] else "LOSER"
        tp_label = ep["tp_sl_result"]
        penalty_parts = []
        if ep["penalties"].get("liq_penalty"):
            penalty_parts.append(f"liq={ep['penalties']['liq_penalty']}")
        if ep["penalties"].get("session_bonus"):
            penalty_parts.append(f"session={ep['penalties']['session_bonus']:+d}")
        if ep["penalties"].get("delta"):
            penalty_parts.append(f"delta={ep['penalties']['delta']:+d}")
        if ep["penalties"].get("violations"):
            penalty_parts.append(f"viol={ep['penalties']['violations']}")
        if ep["penalties"].get("conf_reasons"):
            penalty_parts.append(f"reasons={ep['penalties']['conf_reasons']}")
        penalty_str = " | ".join(penalty_parts) if penalty_parts else "none"

        print(
            f"  #{i:3d}  {ep['coin']:3s}  {ep['ts'].strftime('%Y-%m-%d %H:%M:%S')}  "
            f"px={fmt_price(ep['px']):>12s}  "
            f"score={ep['conf_score']:3d}  min={ep['min_score']:3d}  gap={ep['gap']:3d}  "
            f"regime={ep['regime']:12s}  "
            f"MFE={fmt_pct(ep['mfe_pct']):>9s}  MAE={fmt_pct(ep['mae_pct']):>9s}  "
            f"final={fmt_pct(ep['final_pnl_pct']):>9s}  "
            f"t_mfe={ep['time_to_mfe_s']:5.0f}s  "
            f"[{w_label:6s}]  [{tp_label:7s}]  "
            f"ep_size={ep['episode_size']:3d}  "
            f"penalties: {penalty_str}"
        )

    # -----------------------------------------------------------------------
    # Summary stats
    # -----------------------------------------------------------------------
    print_separator()
    print("  SUMMARY STATISTICS")
    print_separator()

    # Overall
    print_summary_stats("ALL NEAR-MISS EPISODES", top_episodes)

    # By coin
    for coin in COIN_DIRS:
        coin_eps = [e for e in top_episodes if e["coin"] == coin]
        if coin_eps:
            print_summary_stats(f"{coin} EPISODES", coin_eps)

    # By regime
    regimes = set(e["regime"] for e in top_episodes)
    if len(regimes) > 1:
        for regime in sorted(regimes):
            regime_eps = [e for e in top_episodes if e["regime"] == regime]
            if regime_eps:
                print_summary_stats(f"REGIME={regime}", regime_eps)

    # Liq-penalized vs not
    liq_penalized = [e for e in top_episodes if e["has_liq_penalty"]]
    not_liq_penalized = [e for e in top_episodes if not e["has_liq_penalty"]]
    if liq_penalized and not_liq_penalized:
        print_summary_stats("WITH LIQ PENALTY", liq_penalized)
        print_summary_stats("WITHOUT LIQ PENALTY", not_liq_penalized)

    # -----------------------------------------------------------------------
    # Gap buckets: how does performance change with gap size?
    # -----------------------------------------------------------------------
    print_separator()
    print("  PERFORMANCE BY GAP SIZE (distance from threshold)")
    print_separator()

    gap_buckets = defaultdict(list)
    for ep in top_episodes:
        if ep["gap"] <= 5:
            gap_buckets["gap 1-5"].append(ep)
        elif ep["gap"] <= 10:
            gap_buckets["gap 6-10"].append(ep)
        elif ep["gap"] <= 15:
            gap_buckets["gap 11-15"].append(ep)
        elif ep["gap"] <= 20:
            gap_buckets["gap 16-20"].append(ep)
        else:
            gap_buckets["gap 21+"].append(ep)

    for bucket_name in ["gap 1-5", "gap 6-10", "gap 11-15", "gap 16-20", "gap 21+"]:
        if bucket_name in gap_buckets:
            print_summary_stats(bucket_name, gap_buckets[bucket_name])

    # -----------------------------------------------------------------------
    # Score distribution
    # -----------------------------------------------------------------------
    print_separator()
    print("  SCORE DISTRIBUTION OF NEAR-MISSES")
    print_separator()

    score_counts = defaultdict(int)
    for ep in all_episodes:
        score_counts[ep["conf_score"]] += 1

    for score in sorted(score_counts.keys(), reverse=True):
        bar = "#" * min(score_counts[score], 80)
        print(f"    score={score:3d}:  {score_counts[score]:4d}  {bar}")

    # -----------------------------------------------------------------------
    # Verdict
    # -----------------------------------------------------------------------
    print_separator()
    print("  VERDICT")
    print_separator()

    valid_eps = [e for e in top_episodes if e["n_prices"] > 0]
    if valid_eps:
        n_valid = len(valid_eps)
        n_winners = sum(1 for e in valid_eps if e["winner"])
        n_tp = sum(1 for e in valid_eps if e["tp_sl_result"] == "TP_WIN")
        n_sl = sum(1 for e in valid_eps if e["tp_sl_result"] == "SL_LOSS")
        avg_mfe = sum(e["mfe_pct"] for e in valid_eps) / n_valid
        avg_mae = sum(e["mae_pct"] for e in valid_eps) / n_valid

        print(f"  Of {n_valid} near-miss episodes with forward price data:")
        print(f"    {n_winners}/{n_valid} ({n_winners/n_valid*100:.1f}%) had MFE > MAE (simple winner)")
        print(f"    Avg MFE: {fmt_pct(avg_mfe)}, Avg MAE: {fmt_pct(avg_mae)}")
        print()

        if n_tp + n_sl > 0:
            wr = n_tp / (n_tp + n_sl) * 100
            expectancy = (n_tp * 1.0 - n_sl * 0.5) / (n_tp + n_sl)
            print(f"  With 1% TP / 0.5% SL:")
            print(f"    Win rate: {wr:.1f}%  ({n_tp} TP, {n_sl} SL)")
            print(f"    Expectancy: {fmt_pct(expectancy)} per trade")
            if expectancy > 0:
                print(f"    --> PROFITABLE: these missed signals would have made money")
            else:
                print(f"    --> UNPROFITABLE: blocking these was the right call")
        else:
            print(f"  1% TP / 0.5% SL: no trades resolved within {args.forward_minutes}min window")

        print()
        if avg_mfe > avg_mae and n_winners > n_valid * 0.5:
            print(f"  OVERALL: Missed signals WOULD have been net profitable.")
            print(f"  Consider LOWERING the confluence threshold or reducing penalties.")
        else:
            print(f"  OVERALL: Missed signals would NOT have been profitable.")
            print(f"  The current gating is protecting capital correctly.")
    else:
        print("  No forward price data available for analysis.")

    print_separator()


if __name__ == "__main__":
    main()