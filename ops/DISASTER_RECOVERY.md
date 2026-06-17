# Argus Disaster Recovery Plan
# Last updated: 2026-03-23

## Quick Reference
- Repo: https://github.com/Ksmith2322/Argus.git (branch: phase6-hardening)
- IBKR Account: DUP472829 (paper), Gateway port 4002
- Kraken: API keys in .env
- Dashboard: http://localhost:8080
- Python: C:\Argus\.venv\Scripts\python.exe

---

## Scenario 1: PC1 Dies (fresh Windows install)

### Step 1: Install prerequisites
```
1. Python 3.12: https://python.org (check "Add to PATH")
2. Git for Windows: https://git-scm.com
3. IB Gateway: https://www.interactivebrokers.com/en/trading/ib-api.php
```

### Step 2: Clone and set up
```powershell
git clone https://github.com/Ksmith2322/Argus.git C:\Argus\repo
python -m venv C:\Argus\.venv
C:\Argus\.venv\Scripts\activate
cd C:\Argus\repo
pip install pandas numpy requests fastapi uvicorn sse-starlette python-dotenv ib_insync websocket-client scikit-learn joblib matplotlib
```

### Step 3: Verify .env
.env is tracked in git — should be present after clone.
Check: IBKR keys, Kraken keys, Discord webhook all present.

### Step 4: Start IB Gateway
1. Run IB Gateway
2. Select: IB API (not FIX), Paper Trading
3. Log in with IBKR credentials
4. Configure > API > Settings: uncheck "Read-Only API", port 4002

### Step 5: Verify and launch
```powershell
cd C:\Argus\repo
python -m argus_flow.ops.smoke_test          # should be 7/7
python -m argus_flow.ops.config_check        # should be 3/3
.\argus_flow\launch_all.ps1                  # launches fleet
python ops/dashboard.py                       # start dashboard
```

### Step 6: Restore Argus crypto runners (if needed)
```powershell
.\ops\autostart_runner.ps1
```

### What is LOST:
- ops/logs/ (local backtest artifacts)
- argus_flow/logs/ (IBKR runner logs)
- argus_flow/data/ (market data — re-downloadable)

### What is PRESERVED:
- All code (in git)
- All configs (in git)
- .env with API keys (in git)
- Strategy parameters and roadmap

---

## Scenario 2: IBKR Gateway crashes / disconnects

### Fix:
1. Check if IB Gateway process is running
2. If dead: restart IB Gateway, re-authenticate in browser
3. Runners auto-reconnect via ib_insync
4. If runners died: .\argus_flow\launch_all.ps1
5. Check positions: python -m argus_flow.ops.position_monitor

---

## Scenario 3: Runner dies mid-trade

### Fix:
```powershell
python -m argus_flow.ops.position_monitor    # check for mismatches
# If orphaned position: close manually in IBKR TWS GUI
del argus_flow\logs\eurusd\state.json        # reset state (change dir as needed)
python -m argus_flow.runner_eurusd           # restart runner
```

---

## Scenario 4: Data corruption

### State file: delete it, runner starts fresh as FLAT
### Signal/trade CSV: rename to backup, runner creates new
### Config files: git checkout argus_flow/configs/

---

## Scenario 5: Git repo corruption

```powershell
rd /s /q C:\Argus\repo
git clone https://github.com/Ksmith2322/Argus.git C:\Argus\repo
# ops/logs/ and argus_flow/logs/ are NOT in git — local data lost
```

---

## Scenario 6: Move to PC2

```powershell
# PC1: push
git add -A && git commit -m "sync" && git push
# PC2: pull
ssh "ksmith2322@yahoo.com@192.168.1.101"
cd C:\Argus\repo && git pull
.\argus_flow\launch_all.ps1
```

---

## Key Files

| File | Purpose | In Git? |
|------|---------|---------|
| .env | API keys + config | YES |
| argus_flow/configs/*.json | Strategy params | YES |
| argus_flow/logs/*/state.json | Position state | NO |
| argus_flow/logs/*/signals.csv | Signal history | NO |
| argus_flow/logs/*/trades.csv | Trade journal | NO |
| ops/logs/ | Backtest artifacts | NO |

## Key Accounts

| Service | Account | Notes |
|---------|---------|-------|
| IBKR | DUP472829 | Paper, port 4002 |
| Kraken | Keys in .env | $50, research closed |
| GitHub | Ksmith2322/Argus | phase6-hardening branch |
| Discord | Webhook in .env | Trade alerts |