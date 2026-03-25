"""Correlation Guard — prevent correlated exposure on USD pairs.

EUR/USD and GBP/USD are correlated (both USD pairs). Running both simultaneously
without a correlation budget means ~1.5 systems of risk, not 2.

Usage:
    python -m argus_flow.ops.correlation_guard
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# Correlation groups: pairs in the same group share risk
# Per roadmap: EUR/USD + GBP/USD count as 1.5 systems (correlated USD risk)
# JPY crosses share a combined drawdown limit
CORRELATION_GROUPS = {
    "USD_majors": ["eurusd", "gbpusd"],          # both move inverse to USD
    "JPY_crosses": ["eurjpy", "gbpjpy", "audjpy", "cadjpy", "usdjpy"],  # all yen-correlated
}

MAX_CORRELATED_POSITIONS = 1  # max simultaneous positions in same group


def get_positions() -> dict:
    # Class A (active cohort)
    runners = {
        "eurusd": {"name": "EUR/USD", "log_dir": "argus_flow/logs/eurusd"},
        "gbpusd": {"name": "GBP/USD", "log_dir": "argus_flow/logs/gbpusd"},
        "eurjpy": {"name": "EUR/JPY", "log_dir": "argus_flow/logs/eurjpy"},
    }
    # Class B candidates (add when launched)
    for sym, name in [("usdjpy", "USD/JPY"), ("audusd", "AUD/USD"), ("gbpjpy", "GBP/JPY"),
                       ("cadjpy", "CAD/JPY"), ("audjpy", "AUD/JPY")]:
        runners[sym] = {"name": name, "log_dir": f"argus_flow/logs/{sym}"}

    positions = {}
    for key, runner in runners.items():
        state_file = REPO / runner["log_dir"] / "state.json"
        pos = "FLAT"
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text())
                pos = state.get("position", "FLAT")
            except Exception:
                pass
        positions[key] = {"name": runner["name"], "position": pos}

    return positions


def check_correlations(positions: dict) -> list[dict]:
    alerts = []

    for group_name, members in CORRELATION_GROUPS.items():
        in_position = [(k, positions[k]) for k in members if k in positions and positions[k]["position"] != "FLAT"]

        if len(in_position) > MAX_CORRELATED_POSITIONS:
            directions = [p["position"] for _, p in in_position]
            same_dir = len(set(directions)) == 1

            severity = "HIGH" if same_dir else "MEDIUM"
            names = [p["name"] for _, p in in_position]
            dirs = [p["position"] for _, p in in_position]

            alerts.append({
                "group": group_name,
                "severity": severity,
                "message": f"{len(in_position)} correlated positions: {', '.join(f'{n}={d}' for n, d in zip(names, dirs))}",
                "same_direction": same_dir,
                "positions": {k: p["position"] for k, p in in_position},
            })

    return alerts


def main():
    print("=" * 60)
    print(f"  Correlation Guard — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    print("=" * 60)

    positions = get_positions()
    print("\n  Positions:")
    for key, p in positions.items():
        color = "32" if p["position"] == "FLAT" else ("33" if p["position"] == "LONG" else "31")
        print(f"    {p['name']:>10s}: \033[{color}m{p['position']}\033[0m")

    alerts = check_correlations(positions)

    if alerts:
        print(f"\n  \033[33mCORRELATION ALERTS:\033[0m")
        for a in alerts:
            color = "31" if a["severity"] == "HIGH" else "33"
            print(f"    \033[{color}m[{a['severity']}]\033[0m {a['message']}")
            if a["same_direction"]:
                print(f"      Same direction — effectively doubled exposure!")
    else:
        print(f"\n  \033[32mNo correlation alerts. Exposure within limits.\033[0m")

    # Save
    out_path = REPO / "argus_flow" / "logs" / "correlation_check.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "positions": {k: v["position"] for k, v in positions.items()},
        "alerts": alerts,
        "max_correlated": MAX_CORRELATED_POSITIONS,
    }, indent=2, default=str))
    print(f"\n  Saved: {out_path}")


if __name__ == "__main__":
    main()