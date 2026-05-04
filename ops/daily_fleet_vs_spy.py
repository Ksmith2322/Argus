"""ops/daily_fleet_vs_spy — daily fleet-vs-SPY accountability report.

Computes fleet PnL vs SPY return over rolling 1d / 7d / 30d / 90d windows
and sends a Discord summary. The headline number is the SPY DELTA — the
single metric that actually matters per 5/3 strategy review:

  "If we are performing less than the SPY then we are losing.
   Short term capital gains and fees will destroy returns."

Output:
  argus_flow/logs/fleet_vs_spy_latest.json — for dashboard panel
  Discord embed at market close (21:00 UTC ish)

Methodology:
  - Fleet PnL: sum of all closed-trade pnl_usd from all forge/argus trades.csv
    in the window. Doesn't include open-position MTM.
  - SPY return: yfinance daily closes, % change over window, applied to the
    fleet anchor as if you'd bought $X of SPY on day 1.
  - Delta (pp): fleet_pct - spy_pct. Negative = bot is losing to buy-and-hold.

Usage:
    cd c:/Argus/repo
    C:/Argus/.venv/Scripts/python.exe -m ops.daily_fleet_vs_spy

Schedule: daily via managed_truth_loop's once-per-UTC-day slot.
"""
from __future__ import annotations

import csv
import json
import os
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT_PATH = REPO / "argus_flow" / "logs" / "fleet_vs_spy_latest.json"

WINDOWS = {
    "1d": 1,
    "7d": 7,
    "30d": 30,
    "90d": 90,
}


def _post_discord(title: str, body: str, color: int) -> bool:
    import urllib.request, urllib.error
    env_path = REPO / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        return False
    payload = {"embeds": [{
        "title": title, "description": body[:1900], "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }]}
    try:
        req = urllib.request.Request(
            webhook, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def _parse_ts(raw: str):
    try:
        dt = datetime.fromisoformat(str(raw or "").replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _scan_fleet_pnl(window_days: int) -> tuple[float, dict[str, float], int]:
    """Sum closed-trade pnl_usd across all forge + argus trades.csv files in
    the rolling window. Returns (total_pnl, per_strategy_pnl, n_trades).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    total = 0.0
    per_strat: dict[str, float] = defaultdict(float)
    n = 0

    for trades_path in (
        list((REPO / "forge" / "logs").glob("*/trades.csv"))
        + list((REPO / "argus_flow" / "logs").glob("*/trades.csv"))
    ):
        strat = trades_path.parent.name
        try:
            with trades_path.open(encoding="utf-8") as f:
                rdr = csv.DictReader(f)
                for row in rdr:
                    ts_raw = (
                        row.get("ts") or row.get("entry_date")
                        or row.get("entry_ts") or row.get("exit_date") or ""
                    )
                    ts = _parse_ts(ts_raw)
                    if not ts or ts < cutoff:
                        continue
                    pnl = 0.0
                    for k in ("pnl_usd", "pnl", "net_pnl_usd", "pnl_pct_of_fleet"):
                        if k in row and row[k]:
                            try:
                                pnl = float(row[k])
                                break
                            except Exception:
                                pass
                    total += pnl
                    per_strat[strat] += pnl
                    n += 1
        except Exception:
            continue
    return total, dict(per_strat), n


def _spy_return_pct(window_days: int) -> float | None:
    """Buy-and-hold SPY return over window. Uses yfinance daily closes."""
    try:
        import yfinance as yf
    except ImportError:
        return None
    period_lookup = {1: "5d", 7: "1mo", 30: "3mo", 90: "6mo"}
    period = period_lookup.get(window_days, "6mo")
    try:
        df = yf.download("SPY", period=period, interval="1d",
                         progress=False, auto_adjust=True)
        if df is None or df.empty:
            return None
        if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
            df.columns = df.columns.get_level_values(0)
        close = df["Close"]
        if hasattr(close, "ndim") and close.ndim == 2:
            close = close.iloc[:, 0]
        # Closest bar at start of window vs last bar
        cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
        # df.index is timezone-naive; treat as UTC for comparison
        first_idx = None
        for i, idx in enumerate(close.index):
            idx_utc = idx.tz_localize("UTC") if idx.tzinfo is None else idx.tz_convert("UTC")
            if idx_utc >= cutoff:
                first_idx = i
                break
        if first_idx is None:
            return None
        start_px = float(close.iloc[first_idx])
        end_px = float(close.iloc[-1])
        if start_px <= 0:
            return None
        return (end_px - start_px) / start_px * 100.0
    except Exception:
        return None


def _read_anchor_usd() -> float:
    """Read fleet anchor (current broker equity) from gateway_status, fallback to 0."""
    try:
        # Read from the dashboard endpoint cache if available
        snap = REPO / "argus_flow" / "logs" / "_broker" / "broker_snapshot.json"
        if snap.exists():
            d = json.loads(snap.read_text(encoding="utf-8"))
            netliq = (d.get("account") or {}).get("net_liquidation_usd")
            if netliq:
                return float(netliq)
    except Exception:
        pass
    return 0.0


def main() -> int:
    anchor = _read_anchor_usd()

    rows = {}
    for label, days in WINDOWS.items():
        fleet_pnl, per_strat, n = _scan_fleet_pnl(days)
        spy_pct = _spy_return_pct(days)
        fleet_pct = (fleet_pnl / anchor * 100.0) if anchor > 0 else 0.0
        delta_pp = fleet_pct - spy_pct if spy_pct is not None else None

        # Top contributors / draggers (1d window only — daily breakdown is the most actionable)
        top_contrib = []
        worst_drag = []
        if label == "1d" and per_strat:
            sorted_strats = sorted(per_strat.items(), key=lambda x: x[1], reverse=True)
            top_contrib = [(s, p) for s, p in sorted_strats if p > 0][:3]
            worst_drag = [(s, p) for s, p in sorted_strats if p < 0][:3]

        rows[label] = {
            "fleet_pnl_usd": round(fleet_pnl, 2),
            "fleet_return_pct": round(fleet_pct, 3),
            "spy_return_pct": round(spy_pct, 2) if spy_pct is not None else None,
            "delta_pp": round(delta_pp, 2) if delta_pp is not None else None,
            "n_trades": n,
            "top_contributors": [{"strategy": s, "pnl_usd": round(p, 2)} for s, p in top_contrib],
            "worst_draggers": [{"strategy": s, "pnl_usd": round(p, 2)} for s, p in worst_drag],
        }

    payload = {
        "evaluated_at_utc": datetime.now(timezone.utc).isoformat(),
        "anchor_usd": round(anchor, 2),
        "windows": rows,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # Console
    print("=" * 76)
    print(f"  FLEET vs SPY  -  anchor=${anchor:.2f}")
    print("=" * 76)
    print(f"  {'window':<6}  {'n':>4s}  {'fleet$':>10s}  {'fleet%':>8s}  {'SPY%':>7s}  {'delta_pp':>10s}")
    print("  " + "-" * 60)
    for label, days in WINDOWS.items():
        r = rows[label]
        spy_s = f"{r['spy_return_pct']:+.2f}%" if r["spy_return_pct"] is not None else "  --"
        delta_s = f"{r['delta_pp']:+.2f}pp" if r["delta_pp"] is not None else "  --"
        print(f"  {label:<6}  {r['n_trades']:>4}  ${r['fleet_pnl_usd']:>+9.2f}  "
              f"{r['fleet_return_pct']:>+7.3f}%  {spy_s:>7s}  {delta_s:>10s}")
    print()

    # Discord — daily summary at end of US session.
    # Headline: 1d delta. Detail: per-strategy contribution.
    one_d = rows["1d"]
    delta_1d = one_d.get("delta_pp")
    if delta_1d is None:
        return 0  # no SPY data, no Discord
    color = 65280 if delta_1d >= 0 else 16711680  # green/red
    if abs(delta_1d) < 0.5:
        color = 16776960  # yellow if within +/-0.5pp
    sign = "+" if delta_1d >= 0 else ""
    title = f"Daily fleet vs SPY: {sign}{delta_1d:.2f}pp"
    body_lines = [
        f"**Today**  fleet: {one_d['fleet_return_pct']:+.3f}%  SPY: {one_d['spy_return_pct']:+.2f}%  delta: {sign}{delta_1d:.2f}pp",
        f"**7d**     fleet: {rows['7d']['fleet_return_pct']:+.2f}%  SPY: {rows['7d'].get('spy_return_pct',0):+.2f}%  delta: {rows['7d'].get('delta_pp', 0):+.2f}pp",
        f"**30d**    fleet: {rows['30d']['fleet_return_pct']:+.2f}%  SPY: {rows['30d'].get('spy_return_pct',0):+.2f}%  delta: {rows['30d'].get('delta_pp', 0):+.2f}pp",
        f"**90d**    fleet: {rows['90d']['fleet_return_pct']:+.2f}%  SPY: {rows['90d'].get('spy_return_pct',0):+.2f}%  delta: {rows['90d'].get('delta_pp', 0):+.2f}pp",
    ]
    if one_d["top_contributors"]:
        top = ", ".join(f"{c['strategy']} ${c['pnl_usd']:+.0f}" for c in one_d["top_contributors"])
        body_lines.append(f"\n**Top:** {top}")
    if one_d["worst_draggers"]:
        bot = ", ".join(f"{d['strategy']} ${d['pnl_usd']:+.0f}" for d in one_d["worst_draggers"])
        body_lines.append(f"**Drag:** {bot}")
    _post_discord(title, "\n".join(body_lines), color)

    print(f"  Output: {OUT_PATH.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
