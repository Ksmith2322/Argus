"""Merge PC1 + PC2 surviving summaries + match labels from queue_results.log."""
import json
import re
from pathlib import Path

LOGS = Path("C:/Argus/repo/ops/logs")

def load_labels(log_path):
    """Parse queue_results.log for DONE: label | run_id=xxx mappings."""
    labels = {}
    if not log_path.exists():
        return labels
    for line in log_path.read_text(errors="replace").splitlines():
        m = re.search(r"DONE:\s*(\S+)\s*\|\s*run_id=(\S+)", line)
        if m:
            labels[m.group(2)] = m.group(1)
    return labels

# Load labels from both machines
labels = load_labels(LOGS / "queue_results.log")
# PC2 log (if we have it)
pc2_log = Path("C:/Argus/repo/ops/logs/pc2_queue_results.log")
if pc2_log.exists():
    labels.update(load_labels(pc2_log))

# Load PC1 summaries
summaries = []
for f in LOGS.glob("bt_summary_bt_*.json"):
    if "latest" in f.name:
        continue
    try:
        data = json.loads(f.read_text())
        summaries.append(data)
    except:
        pass

# Load PC2 summaries from raw dump
pc2_raw = LOGS / "pc2_summaries_raw.txt"
if pc2_raw.exists():
    buf = ""
    for line in pc2_raw.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        buf += line
        if line.endswith("}"):
            try:
                data = json.loads(buf)
                summaries.append(data)
                buf = ""
            except:
                buf = ""

# Deduplicate by run_id
seen = set()
unique = []
for s in summaries:
    rid = s.get("run_id", "")
    if rid not in seen:
        seen.add(rid)
        unique.append(s)

# Enrich with labels
for s in unique:
    rid = s.get("run_id", "")
    s["_label"] = labels.get(rid, rid[:25])
    s["_coin"] = s.get("symbol", "").replace("-USD", "")

# Sort by PF
unique.sort(key=lambda x: float(x.get("profit_factor", 0)), reverse=True)

# Print full leaderboard
print(f"\n{'='*110}")
print(f"  COMBINED LEADERBOARD -- {len(unique)} runs (PC1 + PC2)")
print(f"{'='*110}")
print(f"{'Rank':>4} {'Label':<45} {'Coin':<6} {'PF':>7} {'WR%':>6} {'Trades':>7} {'PnL':>10} {'DD%':>6}")
print(f"{'-'*110}")

for i, s in enumerate(unique, 1):
    label = s["_label"][:44]
    coin = s["_coin"]
    pf = float(s.get("profit_factor", 0))
    wr = float(s.get("win_rate_pct", 0))
    trades = int(s.get("trades_closed", 0))
    pnl = float(s.get("pnl_usd", 0))
    dd = float(s.get("max_drawdown_pct", 0))
    marker = "**" if pf >= 1.0 else "  "
    print(f"{i:>4} {label:<45} {coin:<6} {marker}{pf:>5.3f} {wr:>5.1f}% {trades:>7} ${pnl:>8.2f} {dd:>5.2f}%")

# Per-coin best
print(f"\n{'='*110}")
print("  PER-COIN BEST CONFIG:")
coins = {}
for s in unique:
    coin = s["_coin"]
    pf = float(s.get("profit_factor", 0))
    if coin not in coins or pf > coins[coin]["pf"]:
        coins[coin] = {"pf": pf, "label": s["_label"], "wr": float(s.get("win_rate_pct", 0)),
                        "trades": int(s.get("trades_closed", 0)), "pnl": float(s.get("pnl_usd", 0))}

print(f"  {'Coin':<8} {'Best PF':>8} {'WR%':>6} {'Trades':>7} {'PnL':>10} {'Config':<40}")
for coin in sorted(coins.keys(), key=lambda c: coins[c]["pf"], reverse=True):
    c = coins[coin]
    tier = "TRADE" if c["pf"] >= 1.0 else ("WATCH" if c["pf"] >= 0.85 else "SKIP")
    print(f"  {coin:<8} {c['pf']:>8.3f} {c['wr']:>5.1f}% {c['trades']:>7} ${c['pnl']:>8.2f} {c['label']:<40} [{tier}]")

print(f"{'='*110}")

# Tier summary
trade = [c for c, d in coins.items() if d["pf"] >= 1.0]
watch = [c for c, d in coins.items() if 0.85 <= d["pf"] < 1.0]
skip = [c for c, d in coins.items() if d["pf"] < 0.85]
print(f"\n  TRADE tier (PF >= 1.0): {', '.join(trade) if trade else 'none'}")
print(f"  WATCH tier (0.85-1.0): {', '.join(watch) if watch else 'none'}")
print(f"  SKIP tier  (PF < 0.85): {', '.join(skip) if skip else 'none'}")
print()