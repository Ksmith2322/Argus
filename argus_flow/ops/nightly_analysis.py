#!/usr/bin/env python3
"""argus_flow/ops/nightly_analysis.py -- Nightly AI-powered fleet analysis.

Runs 6 analysis jobs and posts a consolidated report to Discord:
  1. LLM Trade Review    — plain-English analysis of today's trades
  2. Governor Accuracy    — gov_score predictions vs actual outcomes
  3. Voter Drift Monitor  — AI overlay weight changes and direction
  4. Feature Shift Detect — are current conditions outside training distribution
  5. Pair Health Scorecard — rolling metrics with kill/promote recommendations
  6. Session Edge Tracker  — profitable hour drift detection

Usage:
    python -m argus_flow.ops.nightly_analysis
    python -m argus_flow.ops.nightly_analysis --no-llm     # skip Ollama
    python -m argus_flow.ops.nightly_analysis --dry-run     # print only, no Discord
"""
import argparse
import csv
import json
import math
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv
load_dotenv(REPO / ".env")

LOGS_DIR = REPO / "argus_flow" / "logs"
DATA_DIR = REPO / "argus_flow" / "data"
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:3b")

FX_PAIRS = ["audjpy", "usdjpy", "gbpusd", "cadjpy"]

# ── Helpers ───────────────────────────────────────────────────────

def _safe_float(v, default=0.0):
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def _load_csv(path):
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _today_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _recent_trades(symbol, days=1):
    """Load trades from the last N days."""
    trades_path = LOGS_DIR / symbol / "trades.csv"
    rows = _load_csv(trades_path)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    recent = []
    for r in rows:
        try:
            ts = datetime.fromisoformat(r.get("ts", ""))
            if ts >= cutoff:
                recent.append(r)
        except (ValueError, TypeError):
            continue
    return recent


def _all_valid_trades(symbol):
    """Load all experiment_valid trades."""
    trades_path = LOGS_DIR / symbol / "trades.csv"
    rows = _load_csv(trades_path)
    return [r for r in rows if r.get("experiment_valid", "").lower() == "true"]


def _recent_signals(symbol, days=1):
    """Load signals from the last N days."""
    sig_path = LOGS_DIR / symbol / "signals.csv"
    rows = _load_csv(sig_path)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    recent = []
    for r in rows:
        try:
            ts = datetime.fromisoformat(r.get("ts", ""))
            if ts >= cutoff:
                recent.append(r)
        except (ValueError, TypeError):
            continue
    return recent


def send_discord(content="", embeds=None):
    if not WEBHOOK_URL:
        return False
    import requests
    payload = {}
    if content:
        payload["content"] = content[:2000]
    if embeds:
        payload["embeds"] = embeds[:10]
    try:
        r = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        return r.status_code in (200, 204)
    except Exception:
        return False


# ── Job 1: LLM Trade Review ──────────────────────────────────────

def _build_claude_prompt(days=1):
    """Build a comprehensive prompt for Claude analysis. Saved to file for manual paste."""
    all_trades = []
    for sym in FX_PAIRS:
        trades = _recent_trades(sym, days=days)
        for t in trades:
            t["_symbol"] = sym.upper()
        all_trades.extend(trades)

    # Gather all context data
    trade_lines = []
    for t in all_trades:
        pnl = _safe_float(t.get("pnl_pips", t.get("pnl_pts", 0)))
        gov = t.get("gov_score", t.get("conviction_score", ""))
        trade_lines.append(
            f"  {t['_symbol']:8s} {t.get('direction','?').upper():5s} | "
            f"PnL: {pnl:+.1f} pip | Exit: {t.get('exit_reason','?'):7s} | "
            f"Dur: {t.get('duration_min','?')}min | "
            f"Hour: {t.get('ts', '?')[11:13]} | "
            f"Spread: {t.get('entry_spread', '?')} | "
            f"MTF: {t.get('mtf_score', t.get('bias_4h', '?'))} | "
            f"Gov: {gov}"
        )

    # Pair health data
    pair_lines = []
    for sym in FX_PAIRS:
        trades = _all_valid_trades(sym)
        if not trades:
            pair_lines.append(f"  {sym.upper()}: no trades")
            continue
        recent = trades[-20:]
        pnls = [_safe_float(t.get("pnl_pips", t.get("pnl_pts", 0))) for t in recent]
        wins = sum(1 for p in pnls if p > 0)
        wr = wins / len(pnls) * 100 if pnls else 0
        total = sum(pnls)
        gross_w = sum(p for p in pnls if p > 0)
        gross_l = abs(sum(p for p in pnls if p <= 0))
        pf = round(gross_w / gross_l, 2) if gross_l > 0 else 999
        exits = defaultdict(int)
        for t in recent:
            exits[t.get("exit_reason", "?")] += 1
        pair_lines.append(
            f"  {sym.upper():8s} | {len(recent):2d} trades | WR {wr:.0f}% | PF {pf} | "
            f"PnL {total:+.1f} | Exits: {dict(exits)}"
        )

    # AI overlay weights
    weight_lines = []
    for sym in FX_PAIRS:
        state_path = LOGS_DIR / sym / "ai_overlay_state.json"
        if not state_path.exists():
            continue
        try:
            state = json.loads(state_path.read_text())
            weights = state.get("weights", {})
            sorted_w = sorted(weights.items(), key=lambda x: x[1], reverse=True)
            top = ", ".join(f"{n}={w:.2f}" for n, w in sorted_w[:3])
            bot = ", ".join(f"{n}={w:.2f}" for n, w in sorted_w[-3:])
            weight_lines.append(f"  {sym.upper()}: top=[{top}] bottom=[{bot}]")
        except Exception:
            continue

    # Governor summary
    gov_lines = []
    for sym in FX_PAIRS:
        signals = _recent_signals(sym, days=days)
        entry_sigs = [s for s in signals if s.get("action") == "ENTRY" and s.get("gov_score")]
        skip_sigs = [s for s in signals if "BLOCKED" in s.get("action", "") and s.get("gov_score")]
        if entry_sigs or skip_sigs:
            gov_lines.append(f"  {sym.upper()}: {len(entry_sigs)} entries scored, {len(skip_sigs)} blocks scored")

    # Signal block reasons
    block_lines = []
    for sym in FX_PAIRS:
        signals = _recent_signals(sym, days=days)
        blocks = defaultdict(int)
        for s in signals:
            action = s.get("action", "")
            if action not in ("ENTRY", "NO_TRIGGER", ""):
                blocks[action] += 1
        if blocks:
            top_blocks = sorted(blocks.items(), key=lambda x: -x[1])[:5]
            block_lines.append(f"  {sym.upper()}: " + ", ".join(f"{k}={v}" for k, v in top_blocks))

    prompt = f"""You are a quantitative trading analyst reviewing an automated FX trading system (Argus).
The system trades 4 pairs (AUDJPY, USDJPY, GBPUSD, CADJPY) using a multi-timeframe strategy with an AI overlay.

## Today's Trades ({len(all_trades)} trades, last {days} day(s))
{chr(10).join(trade_lines) if trade_lines else "  No trades in this period."}

## Pair Health (rolling last 20 trades each)
{chr(10).join(pair_lines)}

## AI Overlay Voter Weights
{chr(10).join(weight_lines) if weight_lines else "  No weight data."}

## Governor Scores
{chr(10).join(gov_lines) if gov_lines else "  No governor scores yet."}

## Signal Blocks (last {days} day(s))
{chr(10).join(block_lines) if block_lines else "  No blocks recorded."}

## Analysis Request
Please provide:
1. **Trade Pattern Analysis** — What patterns do you see in winners vs losers? Direction bias? Session/hour issues?
2. **Pair Assessment** — Which pairs should be kept, killed, or need parameter changes? Why?
3. **AI Overlay Review** — Are the voter weights converging sensibly? Any voters that should be reweighted?
4. **Governor Validation** — If governor scores are available, are SKIP signals actually losing trades?
5. **Signal Block Review** — Are the right signals being blocked? Is the system too restrictive or too loose?
6. **Top 3 Concrete Actions** — Specific, implementable changes ranked by expected impact.

Be direct and specific to THIS data. No generic trading advice."""

    return prompt, all_trades


def job_llm_trade_review(days=1):
    """Generate analysis prompt and optionally run through Ollama."""
    prompt, all_trades = _build_claude_prompt(days=days)

    # Always save the prompt for manual Claude paste
    prompt_path = LOGS_DIR / "claude_analysis_prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")

    if not all_trades:
        return "**Trade Review:** No trades in the last 24h. Prompt saved for when trades accumulate."

    # Try Ollama for an automated quick review
    try:
        req = Request(f"{OLLAMA_URL}/api/version", method="GET")
        urlopen(req, timeout=3)

        # Shorter prompt for Ollama (it can't handle the full analysis request well)
        short_prompt = prompt.split("## Analysis Request")[0] + \
            "\nIn 3-4 sentences: What patterns in winners vs losers? Any hour/direction issues? One concrete suggestion."

        payload = json.dumps({
            "model": OLLAMA_MODEL,
            "prompt": short_prompt,
            "stream": False,
            "options": {"temperature": 0.3, "num_predict": 300},
        }).encode()
        req = Request(f"{OLLAMA_URL}/api/generate", data=payload,
                      headers={"Content-Type": "application/json"}, method="POST")
        resp = json.loads(urlopen(req, timeout=45).read())
        llm_review = resp.get("response", "").strip()
    except Exception:
        llm_review = None

    # Build output
    result = _fallback_trade_review(all_trades)
    if llm_review:
        result += f"\n\n**Ollama Quick Take:**\n{llm_review}"
    result += f"\n\n_Full analysis prompt saved to `argus_flow/logs/claude_analysis_prompt.md` — paste into claude.ai for deep analysis._"
    return result


def _fallback_trade_review(trades):
    """Rule-based review when LLM is unavailable."""
    wins = [t for t in trades if _safe_float(t.get("pnl_pips", t.get("pnl_pts", 0))) > 0]
    losses = [t for t in trades if _safe_float(t.get("pnl_pips", t.get("pnl_pts", 0))) <= 0]
    total_pnl = sum(_safe_float(t.get("pnl_pips", t.get("pnl_pts", 0))) for t in trades)

    lines = [f"**Trade Review ({len(trades)} trades, {len(wins)}W/{len(losses)}L, {total_pnl:+.1f} pips):**"]

    # Direction analysis
    longs = [t for t in trades if t.get("direction", "").lower() == "long"]
    shorts = [t for t in trades if t.get("direction", "").lower() == "short"]
    long_pnl = sum(_safe_float(t.get("pnl_pips", t.get("pnl_pts", 0))) for t in longs)
    short_pnl = sum(_safe_float(t.get("pnl_pips", t.get("pnl_pts", 0))) for t in shorts)
    if longs or shorts:
        lines.append(f"  Longs: {len(longs)} trades, {long_pnl:+.1f} pips | Shorts: {len(shorts)} trades, {short_pnl:+.1f} pips")

    # Exit reason breakdown
    exits = defaultdict(int)
    for t in trades:
        exits[t.get("exit_reason", "unknown")] += 1
    lines.append(f"  Exits: {dict(exits)}")

    # Worst trade
    if losses:
        worst = min(losses, key=lambda t: _safe_float(t.get("pnl_pips", t.get("pnl_pts", 0))))
        lines.append(f"  Worst: {worst.get('_symbol', '?')} {worst.get('direction', '?')} "
                      f"{_safe_float(worst.get('pnl_pips', worst.get('pnl_pts', 0))):+.1f} pips")

    return "\n".join(lines)


# ── Job 2: Governor Accuracy ─────────────────────────────────────

def job_governor_accuracy():
    """Compare gov_score predictions vs actual trade outcomes."""
    scored_trades = []
    for sym in FX_PAIRS:
        signals = _recent_signals(sym, days=7)
        trades = _all_valid_trades(sym)

        # Find entry signals with governor scores
        for s in signals:
            if s.get("action") != "ENTRY":
                continue
            gov_score = _safe_float(s.get("gov_score", ""))
            if gov_score == 0.0 and not s.get("gov_score"):
                continue  # no governor score
            gov_action = s.get("gov_action", "?")
            scored_trades.append({
                "symbol": sym.upper(),
                "gov_score": gov_score,
                "gov_action": gov_action,
                "ts": s.get("ts", ""),
            })

    if not scored_trades:
        return "**Governor:** No scored trades yet. Collecting data..."

    take = [t for t in scored_trades if t["gov_action"] == "TAKE"]
    skip = [t for t in scored_trades if t["gov_action"] == "SKIP"]

    lines = [f"**Governor Accuracy ({len(scored_trades)} scored signals):**"]
    lines.append(f"  TAKE: {len(take)} signals (avg score: {sum(t['gov_score'] for t in take)/max(len(take),1):.2f})")
    lines.append(f"  SKIP: {len(skip)} signals (avg score: {sum(t['gov_score'] for t in skip)/max(len(skip),1):.2f})")
    lines.append(f"  _Need closed trades with gov_score to validate accuracy._")

    return "\n".join(lines)


# ── Job 3: Voter Drift Monitor ───────────────────────────────────

def job_voter_drift():
    """Track AI overlay weight changes across pairs."""
    lines = ["**AI Voter Weights (cross-pair pool):**"]

    # Read pool state
    pool_path = LOGS_DIR / "ai_weight_pool.json"
    if pool_path.exists():
        try:
            pool = json.loads(pool_path.read_text())
            weights = pool.get("pooled_weights", {})
            contributions = pool.get("contributions", {})
            sorted_w = sorted(weights.items(), key=lambda x: x[1], reverse=True)

            lines.append(f"  Fleet: {sum(contributions.values())} trades across {len(contributions)} pairs")
            top3 = ", ".join(f"{n}={w:.2f}" for n, w in sorted_w[:3])
            bot3 = ", ".join(f"{n}={w:.2f}" for n, w in sorted_w[-3:])
            lines.append(f"  Strongest: {top3}")
            lines.append(f"  Weakest: {bot3}")

            # Flag any extreme weights
            extremes = [(n, w) for n, w in sorted_w if w > 2.0 or w < 0.3]
            if extremes:
                lines.append(f"  Warning: {len(extremes)} voter(s) at extreme weights: "
                             + ", ".join(f"{n}={w:.2f}" for n, w in extremes))
        except Exception:
            lines.append("  Pool file unreadable.")
    else:
        lines.append("  No pool data yet.")

    # Per-pair overlay summaries
    for sym in FX_PAIRS:
        state_path = LOGS_DIR / sym / "ai_overlay_state.json"
        if not state_path.exists():
            continue
        try:
            state = json.loads(state_path.read_text())
            n_trades = len(state.get("trade_history", []))
            weights = state.get("weights", {})
            if weights:
                best = max(weights.items(), key=lambda x: x[1])
                worst = min(weights.items(), key=lambda x: x[1])
                lines.append(f"  {sym.upper()}: {n_trades} trades | best={best[0]}({best[1]:.2f}) worst={worst[0]}({worst[1]:.2f})")
        except Exception:
            continue

    return "\n".join(lines)


# ── Job 4: Feature Distribution Shift ────────────────────────────

def job_feature_shift():
    """Check if recent signal features differ from training distribution."""
    lines = ["**Feature Shift Detection:**"]

    # Load governor training stats
    gov_path = DATA_DIR / "fx_governor_features.json"
    if not gov_path.exists():
        lines.append("  No governor model — skipping feature shift analysis.")
        return "\n".join(lines)

    # Check recent signals for key features
    key_features = ["vol_z", "range_accel", "mtf_confidence", "mtf_rsi"]
    feature_vals = defaultdict(list)

    for sym in FX_PAIRS:
        signals = _recent_signals(sym, days=1)
        for s in signals:
            for f in key_features:
                val = _safe_float(s.get(f, ""))
                if val != 0.0 or s.get(f):
                    feature_vals[f].append(val)

    if not any(feature_vals.values()):
        lines.append("  No signal data in last 24h.")
        return "\n".join(lines)

    for f, vals in feature_vals.items():
        if not vals:
            continue
        import numpy as np
        arr = np.array(vals)
        lines.append(f"  {f}: mean={arr.mean():.3f} std={arr.std():.3f} n={len(vals)}")

    return "\n".join(lines)


# ── Job 5: Pair Health Scorecard ─────────────────────────────────

def job_pair_health():
    """Rolling 20-trade metrics per pair with recommendations."""
    lines = ["**Pair Health Scorecard (rolling 20 trades):**"]

    for sym in FX_PAIRS:
        trades = _all_valid_trades(sym)
        if not trades:
            lines.append(f"  {sym.upper()}: No valid trades yet")
            continue

        recent = trades[-20:]  # last 20
        pnls = [_safe_float(t.get("pnl_pips", t.get("pnl_pts", 0))) for t in recent]
        wins = sum(1 for p in pnls if p > 0)
        total_pnl = sum(pnls)
        wr = wins / len(pnls) * 100

        # Profit factor
        gross_win = sum(p for p in pnls if p > 0)
        gross_loss = abs(sum(p for p in pnls if p <= 0))
        pf = round(gross_win / gross_loss, 2) if gross_loss > 0 else 999

        # Max drawdown
        peak = 0
        cum = 0
        max_dd = 0
        for p in pnls:
            cum += p
            peak = max(peak, cum)
            max_dd = max(max_dd, peak - cum)

        # Recommendation
        if len(recent) >= 10 and pf < 0.7:
            rec = "KILL"
        elif len(recent) >= 10 and wr < 35:
            rec = "KILL"
        elif len(recent) >= 20 and pf >= 1.5 and wr >= 55:
            rec = "PROMOTE"
        elif len(recent) < 10:
            rec = "OBSERVE"
        else:
            rec = "HOLD"

        emoji_map = {"KILL": "X", "PROMOTE": ">>", "HOLD": "--", "OBSERVE": ".."}
        lines.append(
            f"  {sym.upper():8s} [{emoji_map.get(rec, '--')}] "
            f"{len(recent):2d} trades | WR {wr:.0f}% | PF {pf} | "
            f"PnL {total_pnl:+.1f} | DD {max_dd:.1f} | {rec}"
        )

    return "\n".join(lines)


# ── Job 6: Session Edge Tracker ──────────────────────────────────

def job_session_edge():
    """Track which hours are producing wins vs losses."""
    lines = ["**Session Edge (last 7 days):**"]
    hour_pnl = defaultdict(list)

    for sym in FX_PAIRS:
        trades = _recent_trades(sym, days=7)
        for t in trades:
            try:
                ts = datetime.fromisoformat(t.get("ts", ""))
                hour = ts.hour
                pnl = _safe_float(t.get("pnl_pips", t.get("pnl_pts", 0)))
                hour_pnl[hour].append(pnl)
            except (ValueError, TypeError):
                continue

    if not hour_pnl:
        lines.append("  No trades in last 7 days.")
        return "\n".join(lines)

    # Sort by total PnL per hour
    hour_stats = []
    for h, pnls in sorted(hour_pnl.items()):
        total = sum(pnls)
        wr = sum(1 for p in pnls if p > 0) / len(pnls) * 100
        hour_stats.append((h, len(pnls), wr, total))

    # Show best and worst hours
    by_pnl = sorted(hour_stats, key=lambda x: x[3], reverse=True)
    best = by_pnl[:3]
    worst = by_pnl[-3:] if len(by_pnl) > 3 else []

    if best:
        lines.append("  Best hours: " + ", ".join(
            f"H{h:02d}({n}t, {wr:.0f}%WR, {pnl:+.1f})" for h, n, wr, pnl in best))
    if worst:
        lines.append("  Worst hours: " + ", ".join(
            f"H{h:02d}({n}t, {wr:.0f}%WR, {pnl:+.1f})" for h, n, wr, pnl in worst))

    return "\n".join(lines)


# ── Main ─────────────────────────────────────────────────────────

def run_analysis(use_llm=True, dry_run=False):
    """Run all analysis jobs and post to Discord."""
    print(f"{'=' * 60}")
    print(f"Nightly Analysis — {_today_str()}")
    print(f"{'=' * 60}")

    sections = []

    # Job 1: LLM Trade Review
    print("\n[1/6] Trade Review...")
    if use_llm:
        sections.append(job_llm_trade_review())
    else:
        # Fallback without LLM
        all_trades = []
        for sym in FX_PAIRS:
            trades = _recent_trades(sym, days=1)
            for t in trades:
                t["_symbol"] = sym.upper()
            all_trades.extend(trades)
        sections.append(_fallback_trade_review(all_trades) if all_trades
                        else "**Trade Review:** No trades in the last 24h.")

    # Job 2: Governor Accuracy
    print("[2/6] Governor Accuracy...")
    sections.append(job_governor_accuracy())

    # Job 3: Voter Drift
    print("[3/6] Voter Drift...")
    sections.append(job_voter_drift())

    # Job 4: Feature Shift
    print("[4/6] Feature Shift...")
    sections.append(job_feature_shift())

    # Job 5: Pair Health
    print("[5/6] Pair Health...")
    sections.append(job_pair_health())

    # Job 6: Session Edge
    print("[6/6] Session Edge...")
    sections.append(job_session_edge())

    # Combine report
    report = f"**Argus Nightly Analysis — {_today_str()}**\n\n" + "\n\n".join(sections)

    print(f"\n{'=' * 60}")
    print(report)
    print(f"{'=' * 60}")

    # Post to Discord
    if not dry_run:
        # Discord has 2000 char limit, split if needed
        if len(report) <= 2000:
            ok = send_discord(report)
        else:
            # Send as multiple messages
            ok = True
            chunks = [report[i:i+1900] for i in range(0, len(report), 1900)]
            for chunk in chunks:
                ok = ok and send_discord(chunk)
                time.sleep(0.5)
        print(f"\nDiscord: {'sent' if ok else 'FAILED'}")
    else:
        print("\n[DRY RUN] Skipping Discord post.")

    # Save report locally
    report_path = LOGS_DIR / f"nightly_analysis_{_today_str()}.txt"
    report_path.write_text(report, encoding="utf-8")
    print(f"Saved: {report_path}")

    return report


def main():
    parser = argparse.ArgumentParser(description="Nightly AI-powered fleet analysis")
    parser.add_argument("--no-llm", action="store_true", help="Skip Ollama LLM analysis")
    parser.add_argument("--dry-run", action="store_true", help="Print only, don't post to Discord")
    args = parser.parse_args()

    run_analysis(use_llm=not args.no_llm, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
