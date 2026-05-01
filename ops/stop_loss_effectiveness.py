"""
Stop-loss effectiveness analyzer for the 3 data-rich forge strategies.

For each of forge_multi_orb, forge_spy_mean_rev, forge_vix_intraday:
  1. Time-to-stop distribution (early/mid/late within intended hold window)
  2. Stop distance vs ATR (consistency with configured stop_atr_mult)
  3. Stop hit rate by entry hour UTC
  4. Win/loss asymmetry (mean R win vs mean R loss, payoff ratio)

Concludes per-strategy: bad stops vs bad entries, with a specific recommendation.

Output:
  - JSON at argus_flow/logs/stop_loss_effectiveness.json
  - Console summary per strategy

Date: 2026-04-30
"""

from __future__ import annotations

import csv
import json
import math
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Any

REPO = Path(__file__).resolve().parents[1]

# Per-strategy expected configuration (from runner.py PARAMS as of 2026-04-30).
STRATEGIES: dict[str, dict[str, Any]] = {
    "forge_multi_orb": {
        "csv": REPO / "forge" / "logs" / "multi_orb" / "trades.csv",
        "stop_atr_mult": 0.5,
        "hold_bars": 12,
        "bar_minutes": 5,  # 5-minute bars
    },
    "forge_spy_mean_rev": {
        "csv": REPO / "forge" / "logs" / "spy_mean_rev" / "trades.csv",
        "stop_atr_mult": 0.6,
        "hold_bars": 6,
        "bar_minutes": 5,
    },
    "forge_vix_intraday": {
        "csv": REPO / "forge" / "logs" / "vix_intraday" / "trades.csv",
        "stop_atr_mult": 0.5,
        "hold_bars": 4,
        "bar_minutes": 15,
    },
}

OUT_JSON = REPO / "argus_flow" / "logs" / "stop_loss_effectiveness.json"


def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _parse_ts(v: str) -> datetime | None:
    if not v:
        return None
    s = str(v).strip()
    # Common forms: "2026-04-23 15:40:00+00:00", "2026-04-23T15:40:00Z"
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _pick_col(row: dict[str, str], *candidates: str) -> str | None:
    for c in candidates:
        if c in row and row[c] not in (None, ""):
            return row[c]
    return None


def _summary(name: str, values: list[float]) -> dict[str, Any]:
    if not values:
        return {"name": name, "n": 0}
    return {
        "name": name,
        "n": len(values),
        "mean": round(mean(values), 4),
        "median": round(median(values), 4),
        "min": round(min(values), 4),
        "max": round(max(values), 4),
        "stdev": round(pstdev(values), 4) if len(values) > 1 else 0.0,
    }


def analyze_strategy(strategy: str, cfg: dict[str, Any]) -> dict[str, Any]:
    csv_path: Path = cfg["csv"]
    expected_mult = cfg["stop_atr_mult"]
    hold_bars = cfg["hold_bars"]
    bar_min = cfg["bar_minutes"]
    intended_hold_min = hold_bars * bar_min

    result: dict[str, Any] = {
        "strategy": strategy,
        "csv": str(csv_path),
        "expected_stop_atr_mult": expected_mult,
        "intended_hold_minutes": intended_hold_min,
        "exists": csv_path.exists(),
        "errors": [],
    }

    if not csv_path.exists():
        result["errors"].append(f"trades.csv not found at {csv_path}")
        return result

    with csv_path.open("r", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    result["total_rows"] = len(rows)
    if not rows:
        result["errors"].append("empty trades.csv")
        return result

    # Filter to experiment_valid trades only (drop dirty ones from analysis).
    valid_rows: list[dict[str, str]] = []
    invalid_rows = 0
    for r in rows:
        ev = r.get("experiment_valid", "true")
        if str(ev).strip().lower() in ("false", "0", "no"):
            invalid_rows += 1
            continue
        valid_rows.append(r)
    result["valid_rows"] = len(valid_rows)
    result["invalid_rows_skipped"] = invalid_rows

    # Bucket exit reasons.
    exit_reasons = Counter()
    for r in valid_rows:
        exit_reasons[(r.get("exit_reason") or "unknown").strip()] += 1
    result["exit_reasons"] = dict(exit_reasons)

    # Stopped-out subset.
    stopped = [r for r in valid_rows if (r.get("exit_reason") or "").strip() == "stop"]
    result["stopped_count"] = len(stopped)

    # ---- 1. Time-to-stop distribution ----
    durations: list[float] = []
    early_pct: list[float] = []
    bucket_counts = {"early_<25%": 0, "mid_25_75%": 0, "late_75_100%": 0, "over_100%": 0}
    for r in stopped:
        d = _to_float(r.get("duration_min"))
        if d is None:
            continue
        durations.append(d)
        pct = d / intended_hold_min if intended_hold_min > 0 else 0.0
        early_pct.append(pct)
        if pct < 0.25:
            bucket_counts["early_<25%"] += 1
        elif pct < 0.75:
            bucket_counts["mid_25_75%"] += 1
        elif pct <= 1.0:
            bucket_counts["late_75_100%"] += 1
        else:
            bucket_counts["over_100%"] += 1

    result["time_to_stop"] = {
        "duration_summary_min": _summary("duration_min", durations),
        "pct_of_intended_hold_summary": _summary("pct_of_intended_hold", early_pct),
        "buckets": bucket_counts,
        "intended_hold_min": intended_hold_min,
    }

    # ---- 2. Stop distance vs ATR ----
    measured_mults: list[float] = []
    skipped_no_atr = 0
    for r in stopped:
        entry_px = _to_float(r.get("entry_px"))
        stop_px = _to_float(r.get("stop_px"))
        atr = _to_float(r.get("atr_entry") or r.get("atr"))
        direction = (r.get("direction") or "").strip().lower()
        if entry_px is None or stop_px is None or atr is None or atr == 0:
            skipped_no_atr += 1
            continue
        if direction == "long":
            dist = entry_px - stop_px
        elif direction == "short":
            dist = stop_px - entry_px
        else:
            dist = abs(entry_px - stop_px)
        measured_mults.append(dist / atr)

    result["stop_distance_vs_atr"] = {
        "measured_atr_mult_summary": _summary("measured_atr_mult", measured_mults),
        "expected_atr_mult": expected_mult,
        "skipped_missing_data": skipped_no_atr,
        "deviation_from_expected_pct": (
            round(100.0 * (mean(measured_mults) - expected_mult) / expected_mult, 2)
            if measured_mults and expected_mult
            else None
        ),
    }

    # ---- 3. Stop hit rate by entry hour UTC ----
    hour_total = Counter()
    hour_stopped = Counter()
    for r in valid_rows:
        ts = _parse_ts(r.get("ts") or r.get("entry_ts") or r.get("entry_date") or "")
        if ts is None:
            continue
        h = ts.hour
        hour_total[h] += 1
        if (r.get("exit_reason") or "").strip() == "stop":
            hour_stopped[h] += 1

    by_hour = {}
    for h in sorted(hour_total.keys()):
        n = hour_total[h]
        s = hour_stopped[h]
        by_hour[f"{h:02d}"] = {
            "trades": n,
            "stops": s,
            "stop_rate": round(s / n, 3) if n else None,
        }
    result["stop_rate_by_hour_utc"] = by_hour

    # Highlight worst/best hours with at least 3 trades.
    candidates = [(h, v) for h, v in by_hour.items() if v["trades"] >= 3]
    if candidates:
        worst = max(candidates, key=lambda kv: kv[1]["stop_rate"])
        best = min(candidates, key=lambda kv: kv[1]["stop_rate"])
        result["worst_hour"] = {"hour": worst[0], **worst[1]}
        result["best_hour"] = {"hour": best[0], **best[1]}
    else:
        result["worst_hour"] = None
        result["best_hour"] = None

    # ---- 4. Win/loss asymmetry (R-multiples) ----
    win_R: list[float] = []
    loss_R: list[float] = []
    raw_R: list[float] = []
    for r in valid_rows:
        pnl = _to_float(r.get("pnl_usd") or r.get("pnl"))
        risk = _to_float(r.get("risk_usd"))
        if pnl is None or risk is None or risk <= 0:
            continue
        R = pnl / risk
        raw_R.append(R)
        if pnl > 0:
            win_R.append(R)
        elif pnl < 0:
            loss_R.append(R)

    n_wins = len(win_R)
    n_losses = len(loss_R)
    n_total_R = n_wins + n_losses
    mean_win_R = mean(win_R) if win_R else 0.0
    mean_loss_R = mean(loss_R) if loss_R else 0.0
    payoff_ratio = (-mean_win_R / mean_loss_R) if mean_loss_R < 0 else None
    win_rate = n_wins / n_total_R if n_total_R else None
    expectancy_R = mean(raw_R) if raw_R else 0.0

    result["win_loss_asymmetry"] = {
        "n_wins": n_wins,
        "n_losses": n_losses,
        "win_rate": round(win_rate, 3) if win_rate is not None else None,
        "mean_winning_R": round(mean_win_R, 3),
        "mean_losing_R": round(mean_loss_R, 3),
        "payoff_ratio_(W/-L)": round(payoff_ratio, 3) if payoff_ratio is not None else None,
        "expectancy_R": round(expectancy_R, 3),
    }

    # ---- Diagnose: bad stops vs bad entries vs OK ----
    findings: list[str] = []
    diagnosis = "INDETERMINATE"
    recommendation = "insufficient data"

    # Heuristic 1: stop placement consistent with config?
    if measured_mults:
        avg_m = mean(measured_mults)
        dev_pct = 100.0 * (avg_m - expected_mult) / expected_mult if expected_mult else 0.0
        if abs(dev_pct) > 25:
            findings.append(
                f"Measured stop ATR mult {avg_m:.2f} deviates {dev_pct:+.1f}% from configured {expected_mult:.2f} (slippage / fill drift)."
            )
        else:
            findings.append(
                f"Stop placement consistent with config ({avg_m:.2f} vs configured {expected_mult:.2f}, dev {dev_pct:+.1f}%)."
            )

    # Heuristic 2: time-to-stop pattern.
    if early_pct:
        early_share = bucket_counts["early_<25%"] / len(early_pct)
        late_share = bucket_counts["late_75_100%"] / len(early_pct)
        if early_share >= 0.5:
            findings.append(
                f"{early_share*100:.0f}% of stops fire in first 25% of intended hold — entries are getting picked off immediately (bad entry price / wrong direction)."
            )
        if late_share >= 0.4:
            findings.append(
                f"{late_share*100:.0f}% of stops fire in last 25% of intended hold — many of these are effectively time-stops, not true stop hits."
            )

    # Heuristic 3: payoff geometry.
    if payoff_ratio is not None:
        if mean_loss_R < -1.2:
            findings.append(
                f"Mean losing R is {mean_loss_R:.2f} (worse than -1.0): stops are slipping past their level — losses are bigger than risked."
            )
        elif mean_loss_R > -0.85:
            findings.append(
                f"Mean losing R is only {mean_loss_R:.2f} (better than -1.0): losses smaller than risk — possible mid-bar exits or favorable slippage."
            )
        else:
            findings.append(
                f"Mean losing R is {mean_loss_R:.2f} — stop discipline holding near the configured -1R."
            )
        if payoff_ratio < 1.0:
            findings.append(
                f"Payoff ratio {payoff_ratio:.2f} < 1.0: even when this strategy wins, the wins are smaller than the losses — target is too tight or trail is cutting winners early."
            )

    # Heuristic 4: synthesize diagnosis.
    early_share = bucket_counts["early_<25%"] / len(early_pct) if early_pct else 0.0
    stop_consistent = bool(measured_mults) and abs(
        100.0 * (mean(measured_mults) - expected_mult) / expected_mult
    ) <= 25 if expected_mult else False
    losing_R = mean_loss_R if loss_R else 0.0

    if early_share >= 0.5 and stop_consistent and -1.15 <= losing_R <= -0.85:
        diagnosis = "BAD ENTRIES"
        recommendation = (
            "the problem isn't stops, it's entries — stops fire fast and clean at the configured distance, "
            "meaning the entry signal is putting trades on the wrong side of immediate flow. "
            "Tighten the entry filter (require stronger confirmation / smaller pullback / regime gate) before touching stops."
        )
    elif (not stop_consistent) and measured_mults and mean(measured_mults) > expected_mult * 1.25:
        diagnosis = "STOP TOO WIDE (slippage)"
        # The measured > expected case means we're losing more per trade than we sized for.
        new_mult = round(expected_mult * 0.85, 2)
        recommendation = (
            f"stops are firing wider than configured — measured ATR mult {mean(measured_mults):.2f} vs expected {expected_mult:.2f}. "
            f"Either the order type is letting price run (use stop-limit or marketable-limit) or tighten config: "
            f"stop_atr_mult {expected_mult} -> {new_mult}."
        )
    elif loss_R and mean_loss_R < -1.2 and payoff_ratio is not None and payoff_ratio < 1.0:
        diagnosis = "STOPS LEAKING + TARGETS TOO TIGHT"
        new_mult = round(expected_mult * 0.8, 2)
        recommendation = (
            f"losses are running past -1R while wins clip at the target — both ends are wrong. "
            f"Tighten stop {expected_mult} -> {new_mult} AND widen target by 1.25x to restore positive geometry."
        )
    elif payoff_ratio is not None and payoff_ratio < 0.9 and -1.15 <= losing_R <= -0.85 and stop_consistent:
        diagnosis = "TARGETS TOO TIGHT (not stops)"
        recommendation = (
            f"stops behave correctly (mean loss {losing_R:.2f}R, fills at configured distance), "
            f"but payoff ratio {payoff_ratio:.2f} < 1.0 means the issue is target_atr_mult, not stop_atr_mult. "
            f"Widen target 1.25-1.5x or replace with a trailing stop."
        )
    elif expectancy_R > 0.05 and stop_consistent:
        diagnosis = "STOPS ARE FINE"
        recommendation = "no stop change needed — expectancy is positive and stops fire at the configured distance."
    else:
        diagnosis = "MIXED"
        recommendation = (
            "no single dominant failure mode. Sample is small or signals are noisy. "
            "Re-evaluate after 5/15 ceremony with more fills."
        )

    result["findings"] = findings
    result["diagnosis"] = diagnosis
    result["recommendation"] = recommendation

    return result


def print_strategy(res: dict[str, Any]) -> None:
    print()
    print("=" * 78)
    print(f"STRATEGY: {res['strategy']}")
    print("=" * 78)

    if res.get("errors"):
        for e in res["errors"]:
            print(f"  ERROR: {e}")
        if not res.get("valid_rows"):
            return

    print(f"  rows: total={res.get('total_rows')} valid={res.get('valid_rows')} "
          f"invalid_skipped={res.get('invalid_rows_skipped')}")
    print(f"  exit_reasons: {res.get('exit_reasons')}")
    print(f"  stopped: {res.get('stopped_count')} (intended hold: {res['intended_hold_minutes']} min)")

    tts = res.get("time_to_stop", {})
    if tts.get("duration_summary_min", {}).get("n"):
        d = tts["duration_summary_min"]
        p = tts["pct_of_intended_hold_summary"]
        print()
        print("  [1] TIME-TO-STOP")
        print(f"      duration_min: n={d['n']} mean={d['mean']:.1f} median={d['median']:.1f} "
              f"min={d['min']:.1f} max={d['max']:.1f}")
        print(f"      pct_of_intended_hold: mean={p['mean']:.2%} median={p['median']:.2%}")
        b = tts["buckets"]
        n = sum(b.values()) or 1
        print(f"      buckets: early<25%={b['early_<25%']} ({b['early_<25%']/n:.0%})  "
              f"mid={b['mid_25_75%']} ({b['mid_25_75%']/n:.0%})  "
              f"late75-100%={b['late_75_100%']} ({b['late_75_100%']/n:.0%})  "
              f">100%={b['over_100%']} ({b['over_100%']/n:.0%})")

    sda = res.get("stop_distance_vs_atr", {})
    if sda.get("measured_atr_mult_summary", {}).get("n"):
        m = sda["measured_atr_mult_summary"]
        print()
        print("  [2] STOP DISTANCE vs ATR")
        print(f"      measured ATR mult: n={m['n']} mean={m['mean']:.3f} "
              f"median={m['median']:.3f} stdev={m['stdev']:.3f}")
        print(f"      expected (config): {sda['expected_atr_mult']}")
        dev = sda.get("deviation_from_expected_pct")
        if dev is not None:
            print(f"      deviation: {dev:+.2f}%")

    print()
    print("  [3] STOP RATE BY HOUR (UTC, hours with >=3 trades)")
    by_hour = res.get("stop_rate_by_hour_utc", {})
    rows = [(h, v) for h, v in by_hour.items() if v["trades"] >= 3]
    rows.sort(key=lambda kv: kv[0])
    for h, v in rows:
        print(f"      {h}:00  trades={v['trades']:3d}  stops={v['stops']:3d}  "
              f"rate={v['stop_rate']:.2%}")
    if res.get("worst_hour"):
        print(f"      worst hour: {res['worst_hour']['hour']}:00 "
              f"({res['worst_hour']['stop_rate']:.2%} of {res['worst_hour']['trades']} trades)")
    if res.get("best_hour"):
        print(f"      best hour:  {res['best_hour']['hour']}:00 "
              f"({res['best_hour']['stop_rate']:.2%} of {res['best_hour']['trades']} trades)")

    wla = res.get("win_loss_asymmetry", {})
    print()
    print("  [4] WIN/LOSS ASYMMETRY")
    print(f"      n_wins={wla.get('n_wins')} n_losses={wla.get('n_losses')} "
          f"win_rate={wla.get('win_rate')}")
    print(f"      mean_winning_R={wla.get('mean_winning_R')}  "
          f"mean_losing_R={wla.get('mean_losing_R')}")
    print(f"      payoff_ratio (-W/L)={wla.get('payoff_ratio_(W/-L)')}  "
          f"expectancy_R={wla.get('expectancy_R')}")

    print()
    print("  *** FINDINGS ***")
    for f in res.get("findings", []):
        print(f"      - {f}")
    print()
    print(f"  >>> DIAGNOSIS: {res.get('diagnosis')}")
    print(f"  >>> RECOMMENDATION: {res.get('recommendation')}")


def main() -> int:
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    out: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "analyzer": "ops/stop_loss_effectiveness.py",
        "strategies": {},
    }

    for name, cfg in STRATEGIES.items():
        try:
            res = analyze_strategy(name, cfg)
        except Exception as e:  # noqa: BLE001
            res = {
                "strategy": name,
                "errors": [f"unhandled exception: {e!r}"],
            }
        out["strategies"][name] = res
        print_strategy(res)

    OUT_JSON.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print()
    print("=" * 78)
    print(f"  JSON written: {OUT_JSON}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
