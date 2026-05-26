"""Weekly vetting rollup -- live PF vs backtest CI per strategy.

For each active strategy in the v29 roster:
  1. Read post-epoch canonical fills (EXIT rows only).
  2. Compute live n, PF, win-rate, total PnL.
  3. Compare against the backtest CI lower bound recorded at ship time.
  4. Rank by deviation; print a marked status table.

Status grades:
  TOO_FEW_FILLS    n < 10            (statistically uninterpretable)
  TRACKING         live PF in [CI_lo, CI_hi]    (working as expected)
  OUTPERFORMING    live PF > CI_hi              (positive surprise)
  UNDERPERFORMING  live PF in [floor, CI_lo)    (warning, monitor)
  FAIL_VS_FLOOR    live PF < promotion floor    (kill candidate)

Run weekly. Output:
  ops/reports/system_audit/vetting_rollup.{md,json}
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_REPO = Path(__file__).resolve().parents[2]
_OUT_DIR = _REPO / "ops" / "reports" / "system_audit"
_FILLS_PATH = _REPO / "argus_flow" / "logs" / "canonical_fills.jsonl"
_EPOCH_PATH = _REPO / "argus_flow" / "configs" / "evidence_epoch.json"

PROMOTION_PF_FLOOR = 1.20

# Per-strategy pre-tax CAGR floor needed to beat SPY net of taxes.
# Derivation: SPY ~10% nominal x (1 - 0.20 LTCG) = 8.0% after-tax.
# Trading strategy at operator's ~37-39% short-term cap-gains rate:
# X x (1 - 0.39) >= 8.0% -> X >= 13.1%. See docs/decisions/
# 2026_08_31_real_money_promotion_gate.md condition #1.
CAGR_FLOOR_BEAT_SPY_AFTER_TAX = 0.13
SHORT_TERM_TAX_RATE = 0.39
SPY_AFTER_TAX_BASELINE = 0.08

# Backtest CI baseline per strategy, populated from the sweep that
# justified shipping. Updated when a strategy's sweep is re-run.
# Source columns: family, pf_point, ci_lower, ci_upper, n_backtest,
# expected_fills_per_year, source_artifact.
BACKTEST_BASELINE = {
    "forge_xs_momentum": {
        "family": "xs_momentum_broad_8", "pf_point": 2.05, "ci_lower": 1.43,
        "ci_upper": 4.03, "n_backtest": 79, "fills_per_year": 12,
        "cagr_pretax": 0.10,
        "source": "ops/reports/system_audit/universe_sweep_20y.md",
    },
    "forge_xs_momentum_sectors": {
        "family": "xs_momentum_spdr_11", "pf_point": 2.50, "ci_lower": 1.44,
        "ci_upper": 4.55, "n_backtest": 60, "fills_per_year": 12,
        "cagr_pretax": 0.12,
        "source": "ops/reports/system_audit/universe_sweep_20y.md",
    },
    "forge_xs_momentum_style": {
        "family": "xs_momentum_style_8", "pf_point": 3.78, "ci_lower": 1.85,
        "ci_upper": 7.40, "n_backtest": 50, "fills_per_year": 12,
        "cagr_pretax": 0.16,
        "source": "ops/reports/system_audit/universe_sweep_20y.md",
    },
    "forge_xs_momentum_legacy15": {
        "family": "xs_momentum_sectors_countries_15", "pf_point": 2.22,
        "ci_lower": 1.38, "ci_upper": 3.85, "n_backtest": 100, "fills_per_year": 12,
        "cagr_pretax": 0.11,
        "source": "ops/reports/system_audit/universe_sweep_20y.md",
    },
    "forge_xs_momentum_style_top3": {
        "family": "xs_momentum_style_8_top3", "pf_point": 5.40,
        "ci_lower": 2.70, "ci_upper": 12.81, "n_backtest": 50, "fills_per_year": 12,
        "cagr_pretax": 0.22,
        "source": "ops/reports/system_audit/universe_sweep_20y.md",
    },
    "forge_xs_momentum_legacy15_regime": {
        "family": "xs_momentum_sectors_countries_15_regime",
        "pf_point": 2.22, "ci_lower": 1.38, "ci_upper": 3.85,
        "n_backtest": 77, "fills_per_year": 9,
        "cagr_pretax": 0.10,
        "source": "ops/reports/system_audit/universe_sweep_20y.md",
    },
    "forge_xs_momentum_global47": {
        "family": "xs_momentum_global_47", "pf_point": 2.33,
        "ci_lower": 1.21, "ci_upper": 4.10, "n_backtest": 60, "fills_per_year": 12,
        "cagr_pretax": 0.12,
        "source": "ops/reports/system_audit/universe_sweep_20y.md",
    },
    "forge_tail_hedge": {
        "family": "tail_hedge_regime", "pf_point": 2.91,
        "ci_lower": 1.40, "ci_upper": 5.85, "n_backtest": 28, "fills_per_year": 8,
        "cagr_pretax": 0.05,
        "source": "ops/reports/system_audit/tail_hedge_20y.md",
    },
    # forge_tom_spy removed 2026-05-26 v31.5: backtest CAGR 3% fails the
    # SPY-net-of-taxes bar (~13% pre-tax CAGR floor) AND drags fleet-combined
    # CAGR below mandatory floor.
    "forge_gld_pm_long": {
        "family": "pm_pattern_intraday", "pf_point": 1.08, "ci_lower": 0.90,
        "ci_upper": 1.30, "n_backtest": 300, "fills_per_year": 150,
        "cagr_pretax": 0.05,
        "source": "ops/reports/system_audit/pm_pattern_sweep.md",
    },
    "forge_uso_pm_long": {
        "family": "pm_pattern_intraday", "pf_point": 1.40, "ci_lower": 1.20,
        "ci_upper": 1.75, "n_backtest": 518, "fills_per_year": 259,
        "cagr_pretax": 0.12,
        "source": "ops/reports/system_audit/pm_pattern_sweep.md",
    },
    # 2026-05-26 v31 CADENCE CULL: removed forge_ewz_breakout, forge_ief_jul_hold,
    # forge_gld_jan_hold, forge_uso_jun_hold, forge_hyg_apr_hold, forge_nov_spy
    # from this baseline. All passed disciplined backtest gate but fail cadence
    # floor (1-2 fills/year = decades to validate). They are in
    # helio.roi_filter.KILLED_STRATEGY_CUTOFFS now.
}

# Strategies that operate as a single coordinated sleeve for sizing/promotion
# purposes. The vetting rollup will aggregate their live fills into one row
# titled by SLEEVE_LABEL, while preserving per-variant detail in the JSON
# output. This prevents the fundamental-law double-counting where 6 highly-
# correlated momentum variants get reported as 6 independent bets.
XS_MOMENTUM_SLEEVE_MEMBERS = [
    "forge_xs_momentum",
    "forge_xs_momentum_sectors",
    "forge_xs_momentum_style",
    "forge_xs_momentum_legacy15",
    "forge_xs_momentum_style_top3",
    "forge_xs_momentum_legacy15_regime",
    "forge_xs_momentum_global47",
]
SLEEVES = {
    "xs_momentum_sleeve": {
        "members": XS_MOMENTUM_SLEEVE_MEMBERS,
        "family": "xs_momentum_combined",
        # Conservative blend: members are highly correlated in drawdowns,
        # so realistic combined CI lower is closer to the single-variant
        # CI lower than the variance-reduced theoretical floor would suggest.
        # Use the worst single-variant CI lower as the sleeve floor.
        "pf_point": 2.43,           # rough weighted average across variants
        "ci_lower": 1.21,           # from global47 (worst member CI lower)
        "ci_upper": 5.40,           # from style_top3 (best member CI upper)
        # Allocation-weighted CAGR: baseline(1.0x*10%) + sectors(0.25x*12%)
        # + style(0.5x*16%) + legacy15(0.25x*11%) + style_top3(0.5x*22%) +
        # legacy15_regime(0.25x*10%) + global47(0.25x*12%) / sum_weights(3.0)
        # = ~14.5% pretax CAGR. Beats SPY net-of-tax bar (13%).
        "cagr_pretax": 0.145,
        "fills_per_year": 70,       # 6 variants * ~12/yr - some overlap
    },
}


def _read_epoch() -> dict:
    """Returns {current_epoch_id, current_epoch_start_ts}. The on-disk
    schema stores the start under epochs[i].started_at, keyed by id."""
    try:
        raw = json.loads(_EPOCH_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"current_epoch_id": None, "current_epoch_start_ts": None}
    current_id = raw.get("current_epoch_id")
    start_ts = None
    for ep in raw.get("epochs", []):
        if ep.get("id") == current_id:
            start_ts = ep.get("started_at")
            break
    return {"current_epoch_id": current_id, "current_epoch_start_ts": start_ts}


def _read_post_epoch_fills(epoch_start_ts: Optional[str]) -> list[dict]:
    if not _FILLS_PATH.exists():
        return []
    fills = []
    for line in _FILLS_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            f = json.loads(line)
        except json.JSONDecodeError:
            continue
        if f.get("side") != "EXIT":
            continue
        if epoch_start_ts and (f.get("ts") or "") < epoch_start_ts:
            continue
        fills.append(f)
    return fills


def _bootstrap_pf(pnls: list[float]) -> tuple[Optional[float], Optional[float]]:
    if not pnls:
        return None, None
    try:
        from helio.bootstrap_stats import bootstrap_profit_factor
        bs = bootstrap_profit_factor(pnls, n_resamples=2000, seed=42)
        return bs.ci_lower, bs.ci_upper
    except Exception:
        return None, None


def _evaluate_strategy(label: str, fills: list[dict]) -> dict:
    base = BACKTEST_BASELINE.get(label, {})
    pnls = [float(f.get("pnl_usd") or 0.0) for f in fills if f.get("strategy") == label]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    n = len(pnls)
    pf = abs(sum(wins) / sum(losses)) if losses else (float("inf") if wins else None)
    wr = (len(wins) / n) if n > 0 else None
    total_pnl = sum(pnls) if pnls else 0.0
    live_ci_lo, live_ci_hi = _bootstrap_pf(pnls)

    # Status grade
    if n < 10:
        status = "TOO_FEW_FILLS"
        reason = f"n={n} < 10"
    elif pf is None:
        status = "TOO_FEW_FILLS"
        reason = "no fills"
    elif pf < PROMOTION_PF_FLOOR:
        status = "FAIL_VS_FLOOR"
        reason = f"live PF {pf:.2f} < floor {PROMOTION_PF_FLOOR}"
    elif base.get("ci_upper") and pf > base["ci_upper"]:
        status = "OUTPERFORMING"
        reason = f"live PF {pf:.2f} > backtest CI upper {base['ci_upper']:.2f}"
    elif base.get("ci_lower") and pf >= base["ci_lower"]:
        status = "TRACKING"
        reason = f"live PF {pf:.2f} in backtest CI"
    elif base.get("ci_lower"):
        status = "UNDERPERFORMING"
        reason = (f"live PF {pf:.2f} below backtest CI lower "
                  f"{base['ci_lower']:.2f}")
    else:
        status = "UNKNOWN_NO_BASELINE"
        reason = "no backtest baseline registered"

    return {
        "strategy": label,
        "family": base.get("family", "unknown"),
        "n_live": n,
        "pf_live": round(pf, 3) if pf is not None and pf != float("inf") else None,
        "win_rate_live": round(wr, 3) if wr is not None else None,
        "total_pnl_usd": round(total_pnl, 2),
        "live_ci_lower": round(live_ci_lo, 3) if live_ci_lo else None,
        "live_ci_upper": round(live_ci_hi, 3) if live_ci_hi else None,
        "backtest_pf_point": base.get("pf_point"),
        "backtest_ci_lower": base.get("ci_lower"),
        "backtest_ci_upper": base.get("ci_upper"),
        "backtest_n": base.get("n_backtest"),
        "backtest_cagr_pretax": base.get("cagr_pretax"),
        "expected_fills_per_year": base.get("fills_per_year"),
        "status": status,
        "reason": reason,
    }


def _rank_key(row: dict) -> tuple:
    """Sort: actionable problems first, then outperformers, then quiet."""
    order = {
        "FAIL_VS_FLOOR": 0,
        "UNDERPERFORMING": 1,
        "OUTPERFORMING": 2,
        "TRACKING": 3,
        "TOO_FEW_FILLS": 4,
        "UNKNOWN_NO_BASELINE": 5,
    }
    return (order.get(row["status"], 6), -(row.get("n_live") or 0))


def _evaluate_sleeve(sleeve_label: str, sleeve_cfg: dict,
                      fills: list[dict]) -> dict:
    """Aggregate live fills across all sleeve members and grade as one row.
    Mirrors _evaluate_strategy but pools pnls from every member."""
    members = sleeve_cfg["members"]
    pnls = [float(f.get("pnl_usd") or 0.0)
            for f in fills if f.get("strategy") in members]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    n = len(pnls)
    pf = abs(sum(wins) / sum(losses)) if losses else (float("inf") if wins else None)
    wr = (len(wins) / n) if n > 0 else None
    total_pnl = sum(pnls) if pnls else 0.0
    live_ci_lo, live_ci_hi = _bootstrap_pf(pnls)
    ci_lo = sleeve_cfg.get("ci_lower")
    ci_hi = sleeve_cfg.get("ci_upper")
    if n < 10:
        status, reason = "TOO_FEW_FILLS", f"n={n} < 10"
    elif pf is None or pf < PROMOTION_PF_FLOOR:
        status, reason = "FAIL_VS_FLOOR", f"sleeve PF {pf} < {PROMOTION_PF_FLOOR}"
    elif ci_hi and pf > ci_hi:
        status, reason = "OUTPERFORMING", f"sleeve PF {pf:.2f} > backtest CI upper {ci_hi:.2f}"
    elif ci_lo and pf >= ci_lo:
        status, reason = "TRACKING", f"sleeve PF {pf:.2f} in backtest CI"
    elif ci_lo:
        status, reason = "UNDERPERFORMING", f"sleeve PF {pf:.2f} below CI lower {ci_lo:.2f}"
    else:
        status, reason = "UNKNOWN_NO_BASELINE", "no backtest baseline"
    return {
        "strategy": f"[SLEEVE] {sleeve_label}",
        "family": sleeve_cfg.get("family", "sleeve"),
        "n_live": n,
        "pf_live": round(pf, 3) if pf is not None and pf != float("inf") else None,
        "win_rate_live": round(wr, 3) if wr is not None else None,
        "total_pnl_usd": round(total_pnl, 2),
        "live_ci_lower": round(live_ci_lo, 3) if live_ci_lo else None,
        "live_ci_upper": round(live_ci_hi, 3) if live_ci_hi else None,
        "backtest_pf_point": sleeve_cfg.get("pf_point"),
        "backtest_ci_lower": ci_lo,
        "backtest_ci_upper": ci_hi,
        "backtest_n": None,
        "backtest_cagr_pretax": sleeve_cfg.get("cagr_pretax"),
        "expected_fills_per_year": sleeve_cfg.get("fills_per_year"),
        "status": status,
        "reason": reason,
        "is_sleeve": True,
        "sleeve_members": members,
    }


def _fleet_combined_cagr(rows: list[dict]) -> dict | None:
    """Allocation-weighted fleet pre-tax CAGR, against the 13% bar."""
    import json as _json
    try:
        afp = _REPO / "argus_flow" / "configs" / "allocation_factors.json"
        factors = _json.loads(afp.read_text(encoding="utf-8")).get("factors", {})
    except Exception:
        return None
    total_alloc = 0.0
    weighted_cagr = 0.0
    contributing = []
    for r in rows:
        cagr = r.get("backtest_cagr_pretax")
        if cagr is None:
            continue
        if r.get("is_sleeve"):
            alloc = sum(float(factors.get(m, 0.0)) for m in r.get("sleeve_members", []))
        else:
            alloc = float(factors.get(r["strategy"], 0.0))
        if alloc <= 0:
            continue
        total_alloc += alloc
        weighted_cagr += cagr * alloc
        contributing.append((r["strategy"], alloc, cagr))
    if total_alloc <= 0:
        return None
    fleet_cagr = weighted_cagr / total_alloc
    fleet_net = fleet_cagr * (1 - SHORT_TERM_TAX_RATE)
    delta_pp = (fleet_net - SPY_AFTER_TAX_BASELINE) * 100
    return {
        "fleet_alloc_sum": round(total_alloc, 2),
        "fleet_cagr_pretax": round(fleet_cagr, 4),
        "fleet_cagr_after_tax": round(fleet_net, 4),
        "spy_baseline_after_tax": SPY_AFTER_TAX_BASELINE,
        "delta_vs_spy_pp": round(delta_pp, 2),
        "passes_mandatory_floor": fleet_cagr >= CAGR_FLOOR_BEAT_SPY_AFTER_TAX,
        "contributing": contributing,
    }


def run_rollup() -> dict:
    epoch = _read_epoch()
    epoch_id = epoch.get("current_epoch_id")
    epoch_start = epoch.get("current_epoch_start_ts")
    fills = _read_post_epoch_fills(epoch_start)
    # Per-strategy rows, but skip members that belong to a sleeve so they
    # don't double-count against the aggregate row.
    sleeve_member_set = set()
    for cfg in SLEEVES.values():
        sleeve_member_set.update(cfg["members"])
    rows = [_evaluate_strategy(label, fills)
            for label in BACKTEST_BASELINE.keys()
            if label not in sleeve_member_set]
    # Add one aggregate row per sleeve
    for sleeve_label, sleeve_cfg in SLEEVES.items():
        rows.append(_evaluate_sleeve(sleeve_label, sleeve_cfg, fills))
    rows.sort(key=_rank_key)
    summary = {
        "FAIL_VS_FLOOR": sum(1 for r in rows if r["status"] == "FAIL_VS_FLOOR"),
        "UNDERPERFORMING": sum(1 for r in rows if r["status"] == "UNDERPERFORMING"),
        "TRACKING": sum(1 for r in rows if r["status"] == "TRACKING"),
        "OUTPERFORMING": sum(1 for r in rows if r["status"] == "OUTPERFORMING"),
        "TOO_FEW_FILLS": sum(1 for r in rows if r["status"] == "TOO_FEW_FILLS"),
    }
    fleet_cagr = _fleet_combined_cagr(rows)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "epoch_id": epoch_id,
        "epoch_start_ts": epoch_start,
        "total_fills_in_epoch": len(fills),
        "promotion_pf_floor": PROMOTION_PF_FLOOR,
        "cagr_floor_beat_spy_after_tax": CAGR_FLOOR_BEAT_SPY_AFTER_TAX,
        "fleet_cagr_check": fleet_cagr,
        "summary": summary,
        "rows": rows,
    }


def render_markdown(report: dict) -> str:
    lines = []
    lines.append("# Weekly vetting rollup -- live PF vs backtest CI")
    lines.append("")
    lines.append(f"Generated: `{report['generated_at']}`")
    lines.append(f"Epoch: `{report['epoch_id']}` (started {report['epoch_start_ts']})")
    lines.append(f"Total EXIT fills in epoch: **{report['total_fills_in_epoch']}**")
    lines.append(f"Promotion PF floor: {report['promotion_pf_floor']}")
    lines.append("")
    s = report["summary"]
    lines.append(f"## Summary: {s['FAIL_VS_FLOOR']} FAIL / {s['UNDERPERFORMING']} UNDER "
                 f"/ {s['TRACKING']} TRACKING / {s['OUTPERFORMING']} OVER / "
                 f"{s['TOO_FEW_FILLS']} TOO_FEW")
    lines.append("")
    fc = report.get("fleet_cagr_check")
    if fc:
        floor_pct = report["cagr_floor_beat_spy_after_tax"] * 100
        verdict = "PASSES" if fc["passes_mandatory_floor"] else "FAILS"
        lines.append(f"## Fleet-combined CAGR vs SPY-net-of-tax bar")
        lines.append("")
        lines.append(f"- Allocation sum (deployed risk): **{fc['fleet_alloc_sum']:.2f}x**")
        lines.append(f"- Allocation-weighted pre-tax CAGR: **{fc['fleet_cagr_pretax']*100:.2f}%**")
        lines.append(f"- After-tax (short-term @ {SHORT_TERM_TAX_RATE*100:.0f}%): "
                     f"**{fc['fleet_cagr_after_tax']*100:.2f}%**")
        lines.append(f"- SPY long-term baseline after-tax: {SPY_AFTER_TAX_BASELINE*100:.1f}%")
        lines.append(f"- Delta vs SPY net: **{fc['delta_vs_spy_pp']:+.2f}pp**  -->  "
                     f"**{verdict}** (mandatory floor pre-tax CAGR >= {floor_pct:.0f}%)")
        lines.append("")
    lines.append("| Strategy | Status | n_live | PF live | PF backtest | CI live | CI backtest | $ PnL | CAGR (pre-tax) | vs SPY net | Family |")
    lines.append("|---|---|---:|---:|---:|---|---|---:|---:|---|---|")
    for r in report["rows"]:
        pf_live = f"{r['pf_live']:.2f}" if r['pf_live'] is not None else "--"
        pf_bt = f"{r['backtest_pf_point']:.2f}" if r['backtest_pf_point'] is not None else "--"
        ci_live = (f"[{r['live_ci_lower']:.2f}, {r['live_ci_upper']:.2f}]"
                   if r['live_ci_lower'] is not None else "--")
        ci_bt = (f"[{r['backtest_ci_lower']:.2f}, {r['backtest_ci_upper']:.2f}]"
                 if r['backtest_ci_lower'] is not None else "--")
        cagr = r.get("backtest_cagr_pretax")
        cagr_s = f"{cagr * 100:.1f}%" if cagr is not None else "--"
        # vs SPY net: (cagr * (1 - tax)) - SPY_after_tax_baseline
        if cagr is not None:
            net = cagr * (1 - SHORT_TERM_TAX_RATE)
            delta = (net - SPY_AFTER_TAX_BASELINE) * 100
            spy_verdict = f"{delta:+.1f}pp" + (" ✓" if delta >= 0 else " ✗")
        else:
            spy_verdict = "--"
        lines.append(
            f"| `{r['strategy']}` | **{r['status']}** | {r['n_live']} | {pf_live} | "
            f"{pf_bt} | {ci_live} | {ci_bt} | {r['total_pnl_usd']:+.2f} | "
            f"{cagr_s} | {spy_verdict} | {r['family']} |"
        )
    lines.append("")
    lines.append("## Action items (highest priority first)")
    lines.append("")
    actionable = [r for r in report["rows"]
                  if r["status"] in ("FAIL_VS_FLOOR", "UNDERPERFORMING")]
    if not actionable:
        lines.append("_None._ All strategies with sufficient fills are tracking or "
                     "outperforming their backtest CI.")
    else:
        for r in actionable:
            lines.append(f"- `{r['strategy']}`: {r['reason']} ($ {r['total_pnl_usd']:+.2f} "
                         f"on n={r['n_live']}). Consider: review live trades, check for "
                         f"execution drift, decide kill vs continue.")
    lines.append("")
    lines.append("## Cadence check (strategies that should have fired but haven't)")
    lines.append("")
    laggers = []
    for r in report["rows"]:
        exp_yr = r.get("expected_fills_per_year") or 0
        if exp_yr <= 0:
            continue
        # Rough expected-by-now: fills/yr / 52 weeks * weeks-since-epoch.
        try:
            epoch_dt = datetime.fromisoformat(report["epoch_start_ts"]
                                              .replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            weeks = max(0.1, (now - epoch_dt).total_seconds() / 86400 / 7)
            expected_so_far = exp_yr / 52.0 * weeks
            if expected_so_far >= 1 and r["n_live"] == 0:
                laggers.append((r["strategy"], expected_so_far))
        except Exception:
            pass
    if not laggers:
        lines.append("_None._ Every strategy that has had time to fire either has fills "
                     "or has a low-cadence cycle that hasn't arrived yet.")
    else:
        for label, expected in laggers:
            lines.append(f"- `{label}`: expected ~{expected:.1f} fills by now, has 0 live. "
                         f"Check runner health + heartbeat.")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    print("=== Vetting rollup ===")
    report = run_rollup()
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = _OUT_DIR / "vetting_rollup.json"
    md_path = _OUT_DIR / "vetting_rollup.md"
    json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    s = report["summary"]
    print(f"Epoch: {report['epoch_id']} (fills since start: {report['total_fills_in_epoch']})")
    print(f"Summary: {s['FAIL_VS_FLOOR']} FAIL, {s['UNDERPERFORMING']} UNDER, "
          f"{s['TRACKING']} TRACKING, {s['OUTPERFORMING']} OVER, "
          f"{s['TOO_FEW_FILLS']} TOO_FEW")
    print(f"Wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
