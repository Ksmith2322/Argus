"""
Instrument correlation analysis for real-money candidate strategies.

Computes 90-day daily-return Pearson correlations between the underlying
instruments traded by potential 5/31 real-money candidates. Flags pairs
with |rho| > 0.6 as high overlap (NOT independent).

Usage:
    python ops/instrument_correlation.py

Outputs:
    - argus_flow/logs/instrument_correlation.json  (machine-readable)
    - argus_flow/logs/instrument_correlation.csv   (correlation matrix)
    - console: grouped matrix + flagged pairs + effective-N analysis

Today: 2026-04-30. Window: 90 calendar days back.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

try:
    import yfinance as yf
except ImportError:
    print("ERROR: yfinance not installed. pip install yfinance", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Strategy -> instrument mapping (proxies where the actual symbol is unfetchable)
# ---------------------------------------------------------------------------

STRATEGY_INSTRUMENTS = {
    "forge_nq_overnight":      ["QQQ"],          # MNQ proxy
    "forge_vix_intraday":      ["UVXY"],
    "forge_gld_pm_long":       ["GLD"],
    "forge_jpy_pm_short":      ["JPY=X"],        # USDJPY
    "forge_nq_london_close":   ["QQQ"],          # MNQ proxy
    "forge_aud_asian_breakout":["AUDUSD=X"],
    "argus_usdjpy":            ["JPY=X"],
    "forge_multi_orb":         ["QQQ"],
    "forge_tom_international": ["EEM", "EFA", "EWJ", "VGK", "FXI", "INDA"],
}

# Asset class grouping for the heatmap layout
ASSET_CLASS = {
    "QQQ":      "equity",
    "UVXY":     "vol",
    "GLD":      "metals",
    "JPY=X":    "fx",
    "AUDUSD=X": "fx",
    "EEM":      "intl_equity",
    "EFA":      "intl_equity",
    "EWJ":      "intl_equity",
    "VGK":      "intl_equity",
    "FXI":      "intl_equity",
    "INDA":     "intl_equity",
}

GROUP_ORDER = ["equity", "vol", "metals", "fx", "intl_equity"]

HIGH_OVERLAP_THRESHOLD = 0.6
WINDOW_DAYS = 90


# ---------------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------------

def fetch_closes(symbols: list[str], start: datetime, end: datetime) -> pd.DataFrame:
    """Fetch daily closes; skip symbols that fail."""
    closes = {}
    for sym in symbols:
        try:
            df = yf.download(
                sym,
                start=start.strftime("%Y-%m-%d"),
                end=end.strftime("%Y-%m-%d"),
                auto_adjust=True,
                progress=False,
                threads=False,
            )
            if df is None or df.empty:
                print(f"  SKIP {sym}: empty response", file=sys.stderr)
                continue
            # yfinance returns MultiIndex columns when a single ticker is
            # passed in some versions; flatten to a single level.
            if df.columns.nlevels > 1:
                df.columns = df.columns.get_level_values(0)
            if "Close" not in df.columns:
                print(f"  SKIP {sym}: no Close column", file=sys.stderr)
                continue
            series = df["Close"].dropna()
            if len(series) < 10:
                print(f"  SKIP {sym}: only {len(series)} rows", file=sys.stderr)
                continue
            closes[sym] = series
            print(f"  OK   {sym}: {len(series)} rows")
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL {sym}: {exc}", file=sys.stderr)
    if not closes:
        raise RuntimeError("No symbols fetched successfully")
    return pd.DataFrame(closes).sort_index()


# ---------------------------------------------------------------------------
# Console rendering
# ---------------------------------------------------------------------------

def _heat_char(rho: float) -> str:
    """ASCII heat marker for the console matrix."""
    if pd.isna(rho):
        return " . "
    a = abs(rho)
    if a >= 0.8:
        return "###" if rho > 0 else "==="
    if a >= 0.6:
        return "##." if rho > 0 else "==."
    if a >= 0.3:
        return ".#." if rho > 0 else ".=."
    return " . "


def render_matrix(corr: pd.DataFrame, ordered: list[str]) -> str:
    """Render correlation matrix as a fixed-width table + heat block."""
    lines = []
    width = 9
    header = " " * 12 + "".join(f"{s:>{width}}" for s in ordered)
    lines.append(header)
    for r in ordered:
        row = f"{ASSET_CLASS.get(r,'?'):<3} {r:<8}"
        for c in ordered:
            v = corr.loc[r, c]
            row += f"{v:>{width}.2f}" if pd.notna(v) else " " * width
        lines.append(row)
    lines.append("")
    lines.append("Heat block (### high+ / === high- / ##. mod+ / ==. mod- / .#. low+ / .=. low- /  .  ~0):")
    lines.append(" " * 12 + "".join(f"{s:>5}" for s in ordered))
    for r in ordered:
        row = f"{ASSET_CLASS.get(r,'?'):<3} {r:<8}"
        for c in ordered:
            v = corr.loc[r, c]
            row += f" {_heat_char(v):>4}"
        lines.append(row)
    return "\n".join(lines)


def flag_high_overlap(corr: pd.DataFrame, threshold: float) -> list[dict]:
    """Return list of (a, b, rho) for off-diagonal pairs with |rho| > threshold."""
    flagged = []
    cols = corr.columns.tolist()
    for i, a in enumerate(cols):
        for b in cols[i + 1 :]:
            rho = corr.loc[a, b]
            if pd.notna(rho) and abs(rho) > threshold:
                flagged.append({"a": a, "b": b, "rho": float(rho)})
    flagged.sort(key=lambda x: abs(x["rho"]), reverse=True)
    return flagged


# ---------------------------------------------------------------------------
# Effective independent positions
# ---------------------------------------------------------------------------

def effective_positions(corr_sub: pd.DataFrame) -> dict:
    """
    Effective N from a correlation matrix using the standard portfolio
    diversification formula:

        var(equal-weight portfolio) = (1/N) + ((N-1)/N) * rho_bar
        N_eff = 1 / var

    rho_bar is the mean off-diagonal correlation (uses absolute value,
    because for risk purposes negative correlation also reduces effective
    independence in only one direction; we report both signed and abs forms).
    """
    n = corr_sub.shape[0]
    if n <= 1:
        return {"n": n, "rho_mean_signed": None, "rho_mean_abs": None,
                "n_eff_signed": float(n), "n_eff_abs": float(n),
                "risk_exposed_pct": 0.0}
    mask = ~pd.np.eye(n, dtype=bool) if hasattr(pd, "np") else None
    # Use numpy directly to avoid the deprecated pd.np
    import numpy as np
    arr = corr_sub.to_numpy()
    off = arr[~np.eye(n, dtype=bool)]
    rho_signed = float(np.nanmean(off))
    rho_abs = float(np.nanmean(np.abs(off)))

    def _neff(rho: float) -> float:
        var = (1.0 / n) + ((n - 1) / n) * rho
        var = max(var, 1e-9)
        return 1.0 / var

    n_eff_signed = _neff(rho_signed)
    n_eff_abs = _neff(rho_abs)
    # "risk exposed" = fraction of capital that is NOT diversified away.
    # If N_eff = N, all capital is independent → 0% overlap.
    # If N_eff = 1, the whole fleet is one bet → (N-1)/N % is "duplicated".
    risk_exposed = 1.0 - (n_eff_abs / n)
    return {
        "n": n,
        "rho_mean_signed": rho_signed,
        "rho_mean_abs": rho_abs,
        "n_eff_signed": n_eff_signed,
        "n_eff_abs": n_eff_abs,
        "risk_exposed_pct": risk_exposed * 100.0,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    today = datetime(2026, 4, 30)
    start = today - timedelta(days=WINDOW_DAYS)

    # Unique symbol universe
    universe: list[str] = []
    for syms in STRATEGY_INSTRUMENTS.values():
        for s in syms:
            if s not in universe:
                universe.append(s)

    print(f"Fetching {len(universe)} symbols, {start.date()} -> {today.date()}")
    closes = fetch_closes(universe, start, today)

    rets = closes.pct_change().dropna(how="all")
    corr = rets.corr(method="pearson")

    # Order columns by asset class group
    ordered = []
    for grp in GROUP_ORDER:
        for sym in corr.columns:
            if ASSET_CLASS.get(sym) == grp and sym not in ordered:
                ordered.append(sym)
    # Append any unknown-class fetched symbols at the end
    for sym in corr.columns:
        if sym not in ordered:
            ordered.append(sym)
    corr = corr.loc[ordered, ordered]

    # Console matrix
    print()
    print("=" * 78)
    print(f"DAILY-RETURN CORRELATION  ({len(rets)} sessions, {start.date()} -> {today.date()})")
    print("=" * 78)
    matrix_text = render_matrix(corr, ordered)
    print(matrix_text)

    # Flagged pairs
    flagged = flag_high_overlap(corr, HIGH_OVERLAP_THRESHOLD)
    print()
    print(f"HIGH-OVERLAP PAIRS  (|rho| > {HIGH_OVERLAP_THRESHOLD}):")
    if not flagged:
        print("  (none)")
    else:
        for f in flagged:
            sign = "+" if f["rho"] > 0 else "-"
            print(f"  {f['a']:<10} <-> {f['b']:<10}  rho = {sign}{abs(f['rho']):.3f}")

    # Per-strategy correlation (aggregate basket strategies via mean returns)
    strat_returns = {}
    for strat, syms in STRATEGY_INSTRUMENTS.items():
        cols = [s for s in syms if s in rets.columns]
        if not cols:
            continue
        strat_returns[strat] = rets[cols].mean(axis=1)
    strat_df = pd.DataFrame(strat_returns)
    strat_corr = strat_df.corr(method="pearson")

    # The critical question: 4-strategy real-money fleet.
    fleet_4 = [
        "forge_nq_overnight",
        "forge_vix_intraday",
        "forge_gld_pm_long",
        "forge_jpy_pm_short",
    ]
    fleet_4 = [s for s in fleet_4 if s in strat_corr.columns]
    fleet_corr = strat_corr.loc[fleet_4, fleet_4]
    fleet_eff = effective_positions(fleet_corr)

    print()
    print("=" * 78)
    print("4-STRATEGY REAL-MONEY FLEET CORRELATION")
    print("  {nq_overnight, vix_intraday, gld_pm_long, jpy_pm_short}")
    print("=" * 78)
    short_names = {s: s.replace("forge_", "").replace("argus_", "") for s in fleet_4}
    fleet_corr_disp = fleet_corr.rename(index=short_names, columns=short_names)
    print(fleet_corr_disp.round(3).to_string())
    print()
    print(f"  N strategies          : {fleet_eff['n']}")
    if fleet_eff["rho_mean_signed"] is not None:
        print(f"  mean off-diag rho     : {fleet_eff['rho_mean_signed']:+.3f}  (signed)")
        print(f"  mean |rho|            : {fleet_eff['rho_mean_abs']:.3f}")
        print(f"  N_eff (signed rho)    : {fleet_eff['n_eff_signed']:.2f}  of {fleet_eff['n']}")
        print(f"  N_eff (abs rho)       : {fleet_eff['n_eff_abs']:.2f}  of {fleet_eff['n']}")
        print(f"  Risk-overlap exposure : {fleet_eff['risk_exposed_pct']:.1f}%  "
              f"({100 - fleet_eff['risk_exposed_pct']:.1f}% truly independent)")

    # ------------------------------------------------------------------
    # Persist outputs
    # ------------------------------------------------------------------
    repo_root = Path(__file__).resolve().parents[1]
    logs = repo_root / "argus_flow" / "logs"
    logs.mkdir(parents=True, exist_ok=True)

    csv_path = logs / "instrument_correlation.csv"
    corr.to_csv(csv_path)

    payload = {
        "as_of": today.strftime("%Y-%m-%d"),
        "window_days": WINDOW_DAYS,
        "sessions": int(len(rets)),
        "instruments": ordered,
        "asset_class": {s: ASSET_CLASS.get(s, "?") for s in ordered},
        "correlation_matrix": {
            r: {c: (None if pd.isna(corr.loc[r, c]) else float(corr.loc[r, c]))
                for c in ordered}
            for r in ordered
        },
        "high_overlap_pairs": flagged,
        "strategy_instruments": STRATEGY_INSTRUMENTS,
        "strategy_correlation": {
            r: {c: (None if pd.isna(strat_corr.loc[r, c]) else float(strat_corr.loc[r, c]))
                for c in strat_corr.columns}
            for r in strat_corr.index
        },
        "real_money_fleet_4": {
            "strategies": fleet_4,
            "correlation": {
                r: {c: float(fleet_corr.loc[r, c]) for c in fleet_corr.columns}
                for r in fleet_corr.index
            },
            "effective_positions": fleet_eff,
        },
    }
    json_path = logs / "instrument_correlation.json"
    json_path.write_text(json.dumps(payload, indent=2, default=str))

    print()
    print(f"Wrote {csv_path}")
    print(f"Wrote {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
