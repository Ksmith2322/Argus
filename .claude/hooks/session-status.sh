#!/bin/bash
# SessionStart hook: report runner status to Claude context
cd /c/Argus/repo

# Check runner alive
runner_count=$(wmic process where "name='python.exe'" get CommandLine 2>/dev/null | grep -c runner_unified || echo 0)

# Signal counts (subtract 1 for header)
gbp_sig=$(wc -l < argus_flow/logs/gbpusd/signals.csv 2>/dev/null || echo 0)
eur_sig=$(wc -l < argus_flow/logs/eurusd/signals.csv 2>/dev/null || echo 0)
epy_sig=$(wc -l < argus_flow/logs/eurjpy/signals.csv 2>/dev/null || echo 0)

# Trade counts (suppress redirect errors on missing files)
gbp_trd=$( [ -f argus_flow/logs/gbpusd/trades.csv ] && wc -l < argus_flow/logs/gbpusd/trades.csv || echo 0)
eur_trd=$( [ -f argus_flow/logs/eurusd/trades.csv ] && wc -l < argus_flow/logs/eurusd/trades.csv || echo 0)
epy_trd=$( [ -f argus_flow/logs/eurjpy/trades.csv ] && wc -l < argus_flow/logs/eurjpy/trades.csv || echo 0)

# Heartbeat freshness
hb_status=""
for pair in gbpusd eurusd eurjpy; do
  hb="argus_flow/logs/$pair/heartbeat.json"
  if [ -f "$hb" ]; then
    age=$(( $(date +%s) - $(stat -c %Y "$hb" 2>/dev/null || echo 0) ))
    if [ "$age" -lt 600 ]; then
      hb_status="$hb_status $pair=OK(${age}s)"
    else
      hb_status="$hb_status $pair=STALE(${age}s)"
    fi
  else
    hb_status="$hb_status $pair=NO_HEARTBEAT"
  fi
done

if [ "$runner_count" -gt 0 ]; then
  status="RUNNER ALIVE ($runner_count processes)"
else
  status="RUNNER DOWN"
fi

msg="ARGUS STATUS: $status | Signals: GBP=$gbp_sig EUR=$eur_sig EURJPY=$epy_sig | Trades: GBP=$gbp_trd EUR=$eur_trd EURJPY=$epy_trd | Heartbeat:$hb_status"

cat <<EOF
{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"$msg"}}
EOF