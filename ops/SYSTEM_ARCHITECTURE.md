# Helio Fleet — System Architecture

**Last Updated:** 2026-04-14
**PDT Status:** ELIMINATED (SEC approved 2026-04-14, effective ~late May 2026)

```
╔══════════════════════════════════════════════════════════════════════════════╗
║                          HELIO FLEET ARCHITECTURE                          ║
║                     13 Systems · 9 Families · 27 Nodes                     ║
╚══════════════════════════════════════════════════════════════════════════════╝


  ┌─────────────────────────────────────────────────────────────────────┐
  │                      DATA SOURCES (Free/$0-9/mo)                   │
  │                                                                     │
  │  RSS Feeds ──┐  GDELT ──┐  yfinance ──┐  QuiverQuant ──┐  FRED ──┐│
  │  (13 feeds)  │  (global) │  (prices +  │  (congress)    │  (macro)││
  │  2-min poll  │  15-min   │  options)   │  6-hr poll     │  daily  ││
  │              │           │             │                │         ││
  │  IBKR TWS ──┘  CBOE ────┘  SEC EDGAR ─┘  FinBERT ──────┘         ││
  │  (live bars)   (VIX curve)  (filings)    (NLP sentiment)          ││
  └──────┬───────┴─────┬─────┴──────┬──────┴───────┬────────┴────┬────┘
         │             │            │              │             │
         ▼             ▼            ▼              ▼             ▼
  ┌─────────────────────────────────────────────────────────────────────┐
  │                     INTELLIGENCE LAYER                             │
  │                                                                     │
  │  ┌─────────────┐  ┌─────────────┐  ┌──────────────┐  ┌──────────┐│
  │  │    ATLAS     │  │   THEMIS    │  │ OPTIONS FLOW │  │   VIX    ││
  │  │  Macro Event │  │ Congress    │  │  Put/Call    │  │  Term    ││
  │  │  Intelligence│  │ Trading    │  │  Ratios      │  │ Structure││
  │  │             │  │ Tracker    │  │  Unusual Vol │  │          ││
  │  │ • Classify  │  │            │  │  Max Pain    │  │ Contango/││
  │  │ • Regime    │  │ • Cluster  │  │  Gamma Bias  │  │ Backwrd  ││
  │  │ • Cascades  │  │ • Big Trade│  │              │  │          ││
  │  │ • FinBERT   │  │ • Unanimou│  │              │  │ NORMAL/  ││
  │  │ • Event Exec│  │            │  │              │  │ CAUTION/ ││
  │  │             │  │ 953 trades │  │ Per-ticker   │  │ WARNING/ ││
  │  │ Regime:     │  │ 12 signals │  │ on-demand    │  │ DANGER   ││
  │  │ RISK_OFF    │  │            │  │              │  │          ││
  │  └──────┬──────┘  └─────┬──────┘  └──────┬───────┘  └────┬─────┘│
  └─────────┼───────────────┼────────────────┼────────────────┼──────┘
            │               │                │                │
            ▼               ▼                ▼                ▼
  ┌─────────────────────────────────────────────────────────────────────┐
  │                                                                     │
  │                    ★ CONVICTION SCORER ★                           │
  │                    (The Fleet Conductor)                            │
  │                                                                     │
  │   For EVERY trade entry across ALL systems:                        │
  │                                                                     │
  │   Atlas Regime ────────┐                                           │
  │   Atlas Cascade ───────┤                                           │
  │   Options Flow ────────┼──▶  SCORE  ──▶  SIZE MULTIPLIER          │
  │   Themis Signal ───────┤              0.25x  LOW  (conflicting)    │
  │   Cross-System ────────┘              0.50x  LOW  (weak)           │
  │                                       0.75x  MED  (standard)       │
  │   "Should I take this trade,          1.00x  MED  (confirmed)      │
  │    and how big?"                      1.50x  HIGH (multi-confirm)  │
  │                                       2.00x  MAX  (all aligned)    │
  │                                                                     │
  └────────────────────────────┬────────────────────────────────────────┘
                               │
      ┌────────────────────────┼────────────────────────┐
      │                        │                        │
      ▼                        ▼                        ▼
  ┌──────────────┐  ┌──────────────────┐  ┌──────────────────────────┐
  │  STOCK/FX    │  │  FUTURES INTRA   │  │  MACRO / EVENT / FLOW    │
  │  SYSTEMS     │  │  DAY SYSTEMS     │  │  SYSTEMS                 │
  │              │  │                  │  │                          │
  │ ┌──────────┐ │  │ ┌──────────────┐ │  │ ┌────────────────────┐  │
  │ │ ARGUS FX │ │  │ │    MAMBA     │ │  │ │  ATLAS EVENT EXEC  │  │
  │ │ 4 pairs  │ │  │ │  NQ + YM     │ │  │ │  Cascade trades    │  │
  │ │ MTF + AI │ │  │ │  5m/1m scalp │ │  │ │  25 ETFs           │  │
  │ │ 10-15/mo │ │  │ │  NY open     │ │  │ │  2-4/mo            │  │
  │ └──────────┘ │  │ │  15-30/mo    │ │  │ └────────────────────┘  │
  │ ┌──────────┐ │  │ └──────────────┘ │  │ ┌────────────────────┐  │
  │ │  TITAN   │ │  │ ┌──────────────┐ │  │ │   VIX MEAN REVERT  │  │
  │ │ Breakout │ │  │ │  CUE BANKS   │ │  │ │  Buy SPY @ VIX>30  │  │
  │ │ Long-only│ │  │ │  US30        │ │  │ │  75% WR, PF 2.40   │  │
  │ │ *No PDT* │ │  │ │  Confluence  │ │  │ │  0-1/mo            │  │
  │ │ 5-10/mo  │ │  │ │  Fib + S/R   │ │  │ └────────────────────┘  │
  │ └──────────┘ │  │ │  1:7 R:R     │ │  │ ┌────────────────────┐  │
  │ ┌──────────┐ │  │ │  10-20/mo    │ │  │ │  SECTOR ROTATION   │  │
  │ │  APOLLO  │ │  │ └──────────────┘ │  │ │  Atlas regime ETFs │  │
  │ │ Earnings │ │  │ ┌──────────────┐ │  │ │  Monthly rebalance │  │
  │ │ Post-ER  │ │  │ │    TORI      │ │  │ │  PF 1.55           │  │
  │ │ *No PDT* │ │  │ │  PL CL GC YM│ │  │ │  3/mo              │  │
  │ │ 4-8/mo   │ │  │ │  4H swing    │ │  │ └────────────────────┘  │
  │ └──────────┘ │  │ │  Trendlines  │ │  │ ┌────────────────────┐  │
  │ ┌──────────┐ │  │ │  Action/Safe │ │  │ │  INDEX REBALANCE   │  │
  │ │  HERMES  │ │  │ │  3-6/mo      │ │  │ │  S&P 500 add/del   │  │
  │ │ Gap trade│ │  │ └──────────────┘ │  │ │  PF 6.65           │  │
  │ │ *No PDT* │ │  │                  │  │ │  1-2/mo            │  │
  │ │ 4-8/mo   │ │  │                  │  │ └────────────────────┘  │
  │ └──────────┘ │  │                  │  │ ┌────────────────────┐  │
  │ ┌──────────┐ │  │                  │  │ │  THEMIS CLUSTER    │  │
  │ │ GDX/GLD  │ │  │                  │  │ │  Congress follows   │  │
  │ │ Pairs    │ │  │                  │  │ │  1-2/mo            │  │
  │ │ PF 1.56  │ │  │                  │  │ └────────────────────┘  │
  │ │ 1-2/mo   │ │  │                  │  │ ┌────────────────────┐  │
  │ └──────────┘ │  │                  │  │ │  COMMODITY CASCADE  │  │
  │              │  │                  │  │ │  Oil→EWZ/EWA/EWC   │  │
  │              │  │                  │  │ │  PF 1.80           │  │
  │              │  │                  │  │ └────────────────────┘  │
  └──────┬───────┘  └────────┬─────────┘  └────────────┬─────────────┘
         │                   │                          │
         └───────────────────┼──────────────────────────┘
                             │
                             ▼
  ┌─────────────────────────────────────────────────────────────────────┐
  │                         IBKR EXECUTION                             │
  │                                                                     │
  │  Paper Account: DUP472829 (port 7497)   Balance: ~$1,001,986      │
  │  Bracket orders: Entry + Stop + Target                             │
  │                                                                     │
  │  Client IDs:                                                        │
  │    Argus=7805  Titan=60  Ares=70  Hermes=80  Apollo=90             │
  │    GDX/GLD=101  Mamba MNQ=102  Mamba MYM=103                      │
  │    Tori PL=104  Tori CL=105  Tori GC=106  Tori YM=107             │
  │    Cue Banks MYM=108  Forge reserved=100-199                       │
  └─────────────────────────────┬───────────────────────────────────────┘
                                │
                                ▼
  ┌─────────────────────────────────────────────────────────────────────┐
  │                      RISK MANAGEMENT                               │
  │                                                                     │
  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐             │
  │  │  Conviction   │  │  Drawdown    │  │  Position    │             │
  │  │  Sizing       │  │  Recovery    │  │  Aging       │             │
  │  │              │  │              │  │              │             │
  │  │ Cross-system │  │ 0-3%: NORMAL │  │ >20d: WARN  │             │
  │  │ correlation  │  │ 3-5%: CAUTION│  │ >40d: FORCE │             │
  │  │ aware        │  │ 5-8%: REDUCED│  │   REVIEW    │             │
  │  │              │  │ 8%+: HALTED │  │              │             │
  │  └──────────────┘  └──────────────┘  └──────────────┘             │
  │                                                                     │
  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐             │
  │  │  Portfolio    │  │  Titan       │  │  Kill        │             │
  │  │  Guard        │  │  Filters     │  │  Switch      │             │
  │  │              │  │              │  │              │             │
  │  │ Family limits│  │ Earnings gate│  │ Emergency    │             │
  │  │ Sector caps  │  │ News filter  │  │ halt all     │             │
  │  │ Overlap check│  │ Sentiment    │  │ trading      │             │
  │  └──────────────┘  └──────────────┘  └──────────────┘             │
  └─────────────────────────────────────────────────────────────────────┘


  ┌─────────────────────────────────────────────────────────────────────┐
  │                       MONITORING & OPS                              │
  │                                                                     │
  │  Fleet Monitor ─── Heartbeats ─── Auto-Restart (13 systems)        │
  │       │                                                             │
  │       ├── Paper Monitor Status   (all systems at a glance)         │
  │       ├── Neural Core Dashboard  (/brain — 27 nodes, 9 families)   │
  │       ├── Fleet Operations Page  (/fleet — trades, P&L, status)    │
  │       ├── Benchmark Tracker      (fleet P&L vs SPY daily)          │
  │       ├── Slippage Measurement   (expected vs actual fills)        │
  │       ├── Auto Friday Review     (12-section report, one command)  │
  │       └── Discord Alerts         (events, signals, cascades)       │
  │                                                                     │
  │  Key Commands:                                                      │
  │    python ops/paper_monitor_status.py        # fleet snapshot      │
  │    python -m forge.auto_review --generate    # weekly review       │
  │    python -m forge.benchmark --report        # vs SPY              │
  │    python -m forge.conviction --ticker MSFT  # conviction check    │
  │    python -m forge.atlas.runner --status     # Atlas intel         │
  │    python -m forge.themis.runner --status    # Congress signals    │
  │    python -m forge.macro_strategies --evaluate  # macro strats     │
  │    python -m forge.index_rebalance --predict # next S&P adds      │
  │    python -m forge.mamba.runner --scan       # NAS100 setups       │
  │    python -m forge.cuebanks.runner --scan    # US30 confluence     │
  │    python -m forge.tori.runner --scan        # commodity swings    │
  │    python -m forge.options_flow --ticker SPY # options analysis    │
  └─────────────────────────────────────────────────────────────────────┘


╔══════════════════════════════════════════════════════════════════════════════╗
║                           DATA FLOW SUMMARY                                ║
║                                                                            ║
║   World Events ──▶ Atlas ──┐                                              ║
║   Congress Trades ──▶ Themis ──┤                                           ║
║   Options Markets ──▶ Flow ──┼──▶ Conviction ──▶ Size Trade ──▶ IBKR     ║
║   Price Action ──▶ Systems ──┤       Score                                ║
║   VIX Curve ──▶ Structure ──┘                                             ║
║                                                                            ║
║   ~60-80 trades/month across 13 independent edges                         ║
║   Each sized 0.25x-2.0x based on cross-system conviction                  ║
║                                                                            ║
║   PDT ELIMINATED: Stock systems now unlimited day trades (eff. ~May 2026) ║
╚══════════════════════════════════════════════════════════════════════════════╝


╔══════════════════════════════════════════════════════════════════════════════╗
║                          STRATEGY TIERS                                    ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                            ║
║  TIER 1 — PROVEN EDGE (PF > 1.3, backtested + validated)                 ║
║    Apollo (PF 2.61)  · GDX/GLD (PF 1.56)  · VIX Revert (PF 2.40)       ║
║    Index Rebalance (PF 6.65)                                              ║
║                                                                            ║
║  TIER 2 — PROBABLE EDGE (PF > 1.0, collecting live data)                 ║
║    Argus FX (PF 1.1-1.3) · Hermes (PF 1.27) · Sector Rotation (1.55)   ║
║    Commodity Cascade (PF 1.80)                                            ║
║                                                                            ║
║  TIER 3 — CONCEPT VALID, TUNING NEEDED (PF < 1.0)                       ║
║    Mamba v2 (PF 0.68) · Tori (PF 0.61) · Cue Banks (PF 0.89)           ║
║    Bounce-only Tori subset: PF 3.56 (small sample)                       ║
║                                                                            ║
║  TIER 4 — INTELLIGENCE (no direct PF, multiplies other systems)          ║
║    Atlas · Themis · Conviction Score · Options Flow · VIX Structure       ║
║                                                                            ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                            ║
║  MONTHLY TRADE VOLUME (projected when fully deployed)                     ║
║                                                                            ║
║  Stock/FX:  Argus 10-15 + Titan 5-10 + Apollo 4-8 + Hermes 4-8          ║
║             + GDX/GLD 1-2 + Themis 1-2 = ~25-45 trades                   ║
║                                                                            ║
║  Futures:   Mamba 15-30 + Cue Banks 10-20 + Tori 3-6 = ~28-56 trades    ║
║                                                                            ║
║  Macro:     Atlas Events 2-4 + VIX 0-1 + Sector 3 + Index 1-2           ║
║             + Commodity 1-2 = ~7-12 trades                                ║
║                                                                            ║
║  TOTAL:     ~60-110 trades/month                                          ║
║  (PDT removal increases stock trades from ~25 to ~45)                     ║
║                                                                            ║
╚══════════════════════════════════════════════════════════════════════════════╝


╔══════════════════════════════════════════════════════════════════════════════╗
║                          FILE STRUCTURE                                    ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                            ║
║  C:\Argus\repo\                                                           ║
║  │                                                                         ║
║  ├── argus_flow/              Argus FX system (4 pairs)                   ║
║  │   ├── runner_unified.py    Main FX runner                              ║
║  │   ├── configs/*.json       Per-pair strategy configs                   ║
║  │   └── logs/*/              Per-pair logs + heartbeats                  ║
║  │                                                                         ║
║  ├── titan/                   Stock breakout system (*no PDT*)            ║
║  │   ├── runner.py            Scanner + execution                         ║
║  │   └── logs/                Trades, orders, heartbeat                   ║
║  │                                                                         ║
║  ├── apollo/                  Earnings drift system (*no PDT*)            ║
║  │   ├── runner.py            ER scanner + drift trader                   ║
║  │   └── logs/                Scans, trades, heartbeat                    ║
║  │                                                                         ║
║  ├── hermes/                  Gap trading system (*no PDT*)               ║
║  │   ├── runner.py            Gap detection + entry                       ║
║  │   └── logs/                Trades, heartbeat                           ║
║  │                                                                         ║
║  ├── ares/                    Monthly rotation (reporting-only)            ║
║  │   └── runner.py            Momentum-ranked ETF rotation                ║
║  │                                                                         ║
║  ├── helio/                   Fleet infrastructure                         ║
║  │   ├── fleet_monitor.py     Heartbeat watchdog + auto-restart (13 sys) ║
║  │   ├── portfolio_guard.py   Cross-family position limits                ║
║  │   ├── fleet_risk.py        Sector exposure monitoring                  ║
║  │   └── ibkr_executor.py     Shared IBKR order submission               ║
║  │                                                                         ║
║  ├── forge/                   New builds (isolated from live)             ║
║  │   │                                                                     ║
║  │   ├── atlas/               Macro event intelligence                    ║
║  │   │   ├── runner.py        Main polling loop (2-min cycle)             ║
║  │   │   ├── fleet_gate.py    Regime → fleet sizing (LOG_ONLY/GATE)      ║
║  │   │   ├── event_executor.py  Cascade → executable trades              ║
║  │   │   ├── vix_structure.py VIX term structure monitor                  ║
║  │   │   ├── sources/         RSS, GDELT, Google Trends pollers          ║
║  │   │   ├── classify/        Keywords, FinBERT, severity                ║
║  │   │   ├── cascade/         Templates (26 data-validated), tracker     ║
║  │   │   ├── regime/          4-dim state machine                        ║
║  │   │   ├── impact/          Backfill, Bayesian model, walk-forward     ║
║  │   │   └── db/              SQLite schema + queries                    ║
║  │   │                                                                     ║
║  │   ├── themis/              Congressional trading tracker               ║
║  │   │   ├── runner.py        Fetch + scan + score loop (6-hr)           ║
║  │   │   ├── fetcher.py       QuiverQuant API                            ║
║  │   │   ├── signals.py       Cluster, big trade, unanimous detection    ║
║  │   │   └── db.py            SQLite + return tracking                   ║
║  │   │                                                                     ║
║  │   ├── mamba/               NAS100/US30 breakout scalping              ║
║  │   │   ├── runner.py        Dual TF (5m bias, 1m entry), NY only      ║
║  │   │   ├── trendlines.py    S/R + trendlines + confluence scoring     ║
║  │   │   └── sizing.py        Confluence-based sizing                    ║
║  │   │                                                                     ║
║  │   ├── cuebanks/            US30 confluence trading                     ║
║  │   │   ├── runner.py        Multi-TF (D/H4 bias, M5 entry)            ║
║  │   │   ├── confluence.py    Fib + S/R + structure + exhaustion         ║
║  │   │   └── sizing.py        Confluence-tiered sizing                   ║
║  │   │                                                                     ║
║  │   ├── tori/                Commodity swing trading                     ║
║  │   │   ├── runner.py        Top-down monthly→4H trendlines             ║
║  │   │   ├── trendlines.py    A+ criteria, bounce/break/retest           ║
║  │   │   └── sizing.py        Grade + setup type sizing                  ║
║  │   │                                                                     ║
║  │   ├── conviction.py        ★ Fleet conductor (0.25x-2.0x sizing)     ║
║  │   ├── options_flow.py      Options chain analysis per ticker          ║
║  │   ├── macro_strategies.py  VIX revert + sector rotation + EWZ        ║
║  │   ├── index_rebalance.py   S&P 500 add/delete scanner                ║
║  │   ├── benchmark.py         Fleet P&L vs SPY                           ║
║  │   ├── slippage.py          Fill quality measurement                   ║
║  │   ├── drawdown_recovery.py 4-level recovery protocol                  ║
║  │   ├── titan_filters.py     Earnings gate + news sentiment             ║
║  │   ├── position_aging.py    Trapped capital scanner                    ║
║  │   ├── auto_review.py       Automated Friday report                    ║
║  │   ├── gdx_gld_runner.py    Pairs trade runner                        ║
║  │   └── gdx_gld_pairs.py     Pairs backtest                            ║
║  │                                                                         ║
║  ├── ops/                     Operational docs + scripts                  ║
║  │   ├── dashboard.py         Web dashboard (/brain + /fleet)            ║
║  │   ├── paper_monitor_status.py  Fleet snapshot CLI                     ║
║  │   ├── SYSTEM_ARCHITECTURE.md   This file                              ║
║  │   ├── ROADMAP_PHASE7.md    Build roadmap                              ║
║  │   ├── ARCHITECTURE_DECISION.md  Control plane decision                ║
║  │   ├── PDT_ELIMINATION.md   PDT rule removal (SEC 2026-04-14)         ║
║  │   ├── PARALLEL_BUILD_PLAN.md    Burn-in build plan                    ║
║  │   └── WEEK_1_REVIEW_PLAN.md     Friday review playbook               ║
║  │                                                                         ║
║  └── forge/data/              Runtime databases (gitignored)             ║
║      ├── atlas.db             Events + impacts + predictions             ║
║      ├── themis.db            Congressional trades + signals             ║
║      └── *.json               Impact matrix, Bayesian state, etc.        ║
║                                                                            ║
╚══════════════════════════════════════════════════════════════════════════════╝


╔══════════════════════════════════════════════════════════════════════════════╗
║                          DASHBOARDS                                        ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                            ║
║  http://localhost:8080/         Main dashboard (Argus FX charts + QA)     ║
║  http://localhost:8080/brain    Neural Core (27 nodes, 9 families)        ║
║  http://localhost:8080/fleet    Fleet Operations (trades, P&L, status)    ║
║                                                                            ║
╚══════════════════════════════════════════════════════════════════════════════╝
```
