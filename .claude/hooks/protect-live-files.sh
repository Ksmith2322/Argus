#!/usr/bin/env bash
# .claude/hooks/protect-live-files.sh
# PreToolUse hook for Edit and Write tools.
#
# Receives tool input JSON on stdin. Checks file_path against protected patterns.
# Exit 0 = allow, Exit 2 = block.

INPUT=$(cat)

# Use Python for all path parsing and checking — avoids bash backslash hell on Windows
python3 - "$INPUT" << 'PYEOF'
import sys, json, os, time

raw_input = sys.argv[1] if len(sys.argv) > 1 else ""

try:
    data = json.loads(raw_input)
    file_path = data.get("file_path", "")
except Exception:
    sys.exit(0)

if not file_path:
    sys.exit(0)

# Normalize: backslash to forward slash, lowercase
norm = file_path.replace("\\", "/").lower()

# ---------------------------------------------------------------
# HARD BLOCK — live runtime artifact files
# ---------------------------------------------------------------
HARD_PATTERNS = [
    "state/runtime_state",
    "ops/logs/live_events",
    "ops/logs/live_signals",
    "ops/logs/eth/live_events",
    "ops/logs/eth/live_signals",
    "ops/logs/btc/live_events",
    "ops/logs/btc/live_signals",
    "ops/logs/fills.csv",
    "ops/logs/account.csv",
    "ops/logs/trade_journal",
]

for pattern in HARD_PATTERNS:
    if pattern in norm:
        print("BLOCKED: This is a live runtime artifact written continuously by the runners.", file=sys.stderr)
        print(f"File: {file_path}", file=sys.stderr)
        print("Stop runners before modifying. Restart: powershell.exe -Command '.\\ops\\launch_multi.ps1'", file=sys.stderr)
        sys.exit(2)

# ---------------------------------------------------------------
# Helper: are runners currently live?
# ---------------------------------------------------------------
def runners_live():
    signals = r"C:\Argus\repo\ops\logs\eth\live_signals.csv"
    try:
        age = time.time() - os.path.getmtime(signals)
        return age < 60
    except Exception:
        return False

live = runners_live()

# ---------------------------------------------------------------
# HARD BLOCK — credentials file while runners are live
# ---------------------------------------------------------------
if ".env.coinbase" in norm:
    if live:
        print("BLOCKED: .env.coinbase holds live API credentials and runners are active.", file=sys.stderr)
        print("Stop runners before editing credentials.", file=sys.stderr)
        sys.exit(2)

# ---------------------------------------------------------------
# SOFT WARN — core code files needing restart
# ---------------------------------------------------------------
RESTART_FILES = [
    "engine.py",
    "runner_live.py",
    "feed_coinbase.py",
    "btc_momentum_guard.py",
    "state.py",
    "config.py",
]

basename = os.path.basename(norm)
for name in RESTART_FILES:
    if basename == name:
        if live:
            print(f"NOTE: Runners are live. Changes to {name} require a restart to take effect.", file=sys.stderr)
            print("After editing: kill runners, then run: powershell.exe -Command '.\\ops\\launch_multi.ps1'", file=sys.stderr)
        sys.exit(0)

# ---------------------------------------------------------------
# SOFT WARN — backtest scripts (env var hygiene reminder)
# ---------------------------------------------------------------
if "run_backtest" in norm or "backtest_queue" in norm:
    print("REMINDER: Before running backtests, ensure env is clean:", file=sys.stderr)
    print("  unset ARGUS_BT_ARTIFACT_DIR", file=sys.stderr)
    sys.exit(0)

sys.exit(0)
PYEOF