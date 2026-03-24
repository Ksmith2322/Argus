# Argus Lessons Learned & Status — 2026-03-24

## Where We Are

### What Works
- **Strategy edge is real**: GBP/USD hit 69% WR, +73.4 pips on day 1 (13 trades)
- **FX runners outperform futures**: FX at 75% WR vs Futures at 29% WR
- **Payoff-first methodology proven**: found viable strategies in hours vs weeks
- **8 of 14 runners were profitable** before infrastructure issues
- **TWS is more stable than IB Gateway** for API connections

### What Doesn't Work
- **IB Gateway drops connections constantly** — killed runners repeatedly, lost trades
- **Too many simultaneous IBKR connections** — 15+ runners overloads the API
- **Background process launches from bash** — zombie processes pile up on Windows, hold client IDs
- **State file restore bug** — stop/target prices weren't restored after reconnect (FIXED)
- **Nikkei (NKD)** — 25% WR, all stops, killed after 8 trades
- **Futures stops too tight at 30bps** — widened to 50bps (untested yet)

### Current Fleet Status
- **TWS connected** on port 7496 (live account U24860535, Read-Only API)
- **1 runner confirmed alive** (EUR/JPY)
- **Others need relaunch** — state files were cleared, runners lost their signal files
- **Dashboard working** but showing empty data

---

## Performance Summary (36 trades before reset)

### Best Performers
| Runner | Trades | WR | PnL |
|---|---|---|---|
| GBP/USD | 13 | 69% | +73.4 pip |
| MYM (Dow) | 2 | 50% | +139.0 pts |
| EUR/USD | 2 | 50% | +19.8 pip |
| USD/JPY | 1 | 100% | +18.3 pip |

### Worst Performers
| Runner | Trades | WR | PnL | Issue |
|---|---|---|---|---|
| NKD (Nikkei) | 8 | 25% | -475 pts | All stops, killed |
| MNQ | 6 | 33% | -274 pts | Bug + stop too tight |
| MGC (Gold) | 4 | 25% | -19.2 pts | Stop too tight |

### Key Metrics
- Fleet total: 36 trades, 50% WR
- FX: 75% WR (strong)
- Futures: 29% WR (weak — stops too tight)
- Winners avg duration: 49 min (hold to timeout)
- Losers avg duration: 25 min (stopped early)
- 48% of exits were stops, 38% timeouts, 14% targets

---

## Lessons Learned

### 1. Infrastructure > Strategy
The strategies work when they can run. Most losses came from:
- Gateway disconnects killing runners mid-trade
- Zombie processes preventing reconnection
- State restore bugs after crashes

**Fix:** TWS instead of Gateway, single-process multi-runner, proper Windows process management.

### 2. FX is the sweet spot
- Low fees (<1bps), structured sessions, strong precursors
- 75% WR across FX pairs
- GBP/USD is the standout — should be the anchor runner

### 3. Futures need wider stops
- 30bps stops on volatile instruments = stopped out before moves develop
- Winners hold ~49 min, losers stopped at ~25 min
- Widened to 50bps but haven't tested yet

### 4. Connection management is critical
- IB Gateway: unstable, drops connections randomly
- TWS: more stable but heavier
- Can't run 15+ simultaneous connections reliably
- Need: single-process multi-runner architecture

### 5. Windows bash background processes create zombies
- `&` background launches don't clean up properly
- PowerShell `Start-Process` with `-WorkingDirectory` is more reliable
- But still creates orphan processes on errors

### 6. State persistence needs to be complete
- Bug: stop_price and target_price weren't saved/restored
- Caused false exits on reconnect ($291 loss from one bug)
- Fixed across all 6 runner types

### 7. The timeout exit IS the edge
- Most winners exit at 60-min timeout with positive drift
- Only 14% of trades hit their profit target
- The strategy is "enter when conditions align, wait for drift"

---

## Next Steps (Priority Order)

### Immediate (today/tomorrow)
1. **Stabilize TWS with 5-8 runners** — focus on proven FX pairs
2. **Let runners collect clean data** — need 30+ trades for real assessment
3. **Monitor TWS stability** — is it better than Gateway?

### This Week
4. **Build single-process multi-runner** — one IBKR connection, all pairs
   - Eliminates connection limit problem permanently
   - Eliminates zombie process problem
   - Most important infrastructure fix
5. **Evaluate futures with 50bps stops** — do wider stops help?
6. **Hit 30 trades on top runners** — GBP/USD, EUR/JPY, MYM

### Next Week
7. **30-trade gate review** — which runners to keep, kill, or adjust
8. **Paper → micro-live decision** for top 2-3 runners
9. **Add more runners** once single-process architecture is stable
10. **Overnight position policy** — close before session end or hold?

### Future (after stability proven)
11. **Scale position sizes** — $2,500 pool, 1-2% risk per trade
12. **Add commodities/crypto** — after core runners proven
13. **$25K equities unlock** — TQQQ, options, swing trades

---

## Architecture Decision: Single-Process Multi-Runner

This is the #1 priority fix. Current architecture:
```
Runner 1 (EUR/USD) → IBKR Connection 1
Runner 2 (GBP/USD) → IBKR Connection 2
Runner 3 (MNQ)     → IBKR Connection 3
...                 → Connection N (limit ~8-10)
```

Target architecture:
```
Multi-Runner Process → IBKR Connection 1
  ├── EUR/USD strategy
  ├── GBP/USD strategy
  ├── GBP/JPY strategy
  ├── MNQ strategy
  ├── MYM strategy
  └── ... (unlimited strategies, 1 connection)
```

Benefits:
- 1 connection instead of 15
- No zombie process problem
- Shared bar data reduces API calls
- Single dashboard data source
- Easier to monitor and restart

This should be the next major build.

---

## Key Numbers to Remember
- GBP/USD: 69% WR, PF 1.67 — the proven edge
- FX: 75% WR overall — the best asset class
- Futures: 29% WR at 30bps stops — needs 50bps+ or different approach
- Winners hold 49min, losers stopped at 25min — timeout is the edge
- IBKR connection limit: ~8-10 simultaneous on TWS
- Live account: U24860535 (Read-Only API for safety)