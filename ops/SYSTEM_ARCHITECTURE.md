# Helio Fleet — System Architecture

**Last Updated:** 2026-04-12

```
╔══════════════════════════════════════════════════════════════════════════════╗
║                          HELIO FLEET ARCHITECTURE                          ║
╚══════════════════════════════════════════════════════════════════════════════╝


  ┌─────────────────────────────────────────────────────────────────────┐
  │                        DATA SOURCES (Free)                         │
  │                                                                     │
  │  RSS Feeds ──┐  GDELT ──┐  yfinance ──┐  QuiverQuant ──┐  FRED ──┐│
  │  (13 feeds)  │  (global) │  (prices)   │  (congress)    │  (macro)││
  │  2-min poll  │  15-min   │  on-demand  │  6-hr poll     │  daily  ││
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
  │  │             │  │            │  │              │  │ CAUTION/ ││
  │  │ Regime:     │  │ 947 trades │  │ Per-ticker   │  │ WARNING/ ││
  │  │ RISK_OFF    │  │ 11 signals │  │ on-demand    │  │ DANGER   ││
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
  │   Themis Signal ───────┤              0.25x  LOW                   │
  │   Cross-System ────────┘              0.75x  MEDIUM                │
  │                                       1.00x  MEDIUM                │
  │   "Should I take this trade,          1.50x  HIGH                  │
  │    and how big?"                      2.00x  MAX                   │
  │                                                                     │
  └────────────────────────────┬────────────────────────────────────────┘
                               │
            ┌──────────────────┼──────────────────┐
            ▼                  ▼                  ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │                      TRADING SYSTEMS (10)                           │
  │                                                                     │
  │  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐     │
  │  │ ARGUS   │ │ TITAN   │ │ APOLLO  │ │ HERMES  │ │ GDX/GLD │     │
  │  │ FX Pairs│ │ Stock   │ │Earnings │ │ Gap     │ │ Pairs   │     │
  │  │         │ │Breakouts│ │ Drift   │ │ Trading │ │ Trade   │     │
  │  │ 4 pairs │ │ Long    │ │ Post-ER │ │ Fade/   │ │ Mean    │     │
  │  │ MTF+AI  │ │ Scanner │ │ Gap+Hold│ │ Continue│ │ Revert  │     │
  │  │ 10-15/mo│ │ 3-5/mo  │ │ 2-4/mo  │ │ 2-4/mo  │ │ 1-2/mo  │     │
  │  └────┬────┘ └────┬────┘ └────┬────┘ └────┬────┘ └────┬────┘     │
  │       │           │           │           │           │           │
  │  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐     │
  │  │ ATLAS   │ │ VIX     │ │ SECTOR  │ │ INDEX   │ │ THEMIS  │     │
  │  │ Event   │ │ Mean    │ │Rotation │ │Rebalance│ │ Cluster │     │
  │  │ Trades  │ │ Revert  │ │         │ │ Scanner │ │ Follows │     │
  │  │         │ │         │ │ Atlas   │ │         │ │         │     │
  │  │ Cascade │ │Buy SPY  │ │ Regime  │ │ S&P 500 │ │Congress │     │
  │  │ Signals │ │VIX > 30 │ │ → ETFs  │ │ Add/Del │ │ Insider │     │
  │  │ 2-4/mo  │ │ 0-1/mo  │ │ 3/mo    │ │ 1-2/mo  │ │ 1-2/mo  │     │
  │  └────┬────┘ └────┬────┘ └────┬────┘ └────┬────┘ └────┬────┘     │
  │       │           │           │           │           │           │
  └───────┼───────────┼───────────┼───────────┼───────────┼───────────┘
          │           │           │           │           │
          ▼           ▼           ▼           ▼           ▼
  ┌─────────────────────────────────────────────────────────────────────┐
  │                         IBKR EXECUTION                             │
  │                                                                     │
  │  Paper Account: DUP472829 (port 7497)                              │
  │  Bracket orders: Entry + Stop + Target                             │
  │  Client IDs: Argus=7805, Titan=60, Ares=70, Hermes=80,           │
  │              Apollo=90, GDX/GLD=101, Forge=100-199                 │
  └─────────────────────────────┬───────────────────────────────────────┘
                                │
                                ▼
  ┌─────────────────────────────────────────────────────────────────────┐
  │                      RISK MANAGEMENT                               │
  │                                                                     │
  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐             │
  │  │  Portfolio    │  │  Drawdown    │  │  Position    │             │
  │  │  Guard        │  │  Recovery    │  │  Aging       │             │
  │  │              │  │              │  │              │             │
  │  │ Cross-family │  │ 0-3%: NORMAL │  │ >20d: WARN  │             │
  │  │ position     │  │ 3-5%: CAUTION│  │ >40d: FORCE │             │
  │  │ limits       │  │ 5-8%: REDUCED│  │   REVIEW    │             │
  │  │ Sector caps  │  │ 8%+: HALTED │  │              │             │
  │  └──────────────┘  └──────────────┘  └──────────────┘             │
  │                                                                     │
  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐             │
  │  │  Titan       │  │  Fleet       │  │  Kill        │             │
  │  │  Filters     │  │  Risk        │  │  Switch      │             │
  │  │              │  │              │  │              │             │
  │  │ Earnings gate│  │ Sector       │  │ Emergency    │             │
  │  │ News filter  │  │ exposure     │  │ halt all     │             │
  │  │ Sentiment    │  │ monitoring   │  │ trading      │             │
  │  └──────────────┘  └──────────────┘  └──────────────┘             │
  └─────────────────────────────────────────────────────────────────────┘


  ┌─────────────────────────────────────────────────────────────────────┐
  │                       MONITORING & OPS                              │
  │                                                                     │
  │  Fleet Monitor ─── Heartbeats ─── Auto-Restart                     │
  │       │                                                             │
  │       ├── Paper Monitor Status (all systems at a glance)           │
  │       ├── Benchmark Tracker (fleet P&L vs SPY daily)               │
  │       ├── Slippage Measurement (expected vs actual fills)          │
  │       ├── Auto Friday Review (12-section report, one command)      │
  │       └── Discord Alerts (events, signals, cascade forecasts)      │
  │                                                                     │
  │  Commands:                                                          │
  │    python ops/paper_monitor_status.py          # fleet snapshot    │
  │    python -m forge.auto_review --generate      # weekly review     │
  │    python -m forge.benchmark --report          # vs SPY            │
  │    python -m forge.conviction --ticker MSFT    # conviction check  │
  │    python -m forge.atlas.runner --status       # Atlas intel       │
  │    python -m forge.themis.runner --status      # Congress signals  │
  │    python -m forge.macro_strategies --evaluate  # macro strategies │
  │    python -m forge.index_rebalance --predict   # next S&P adds    │
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
║   25-40 trades/month across 10 independent edges                          ║
║   Each sized 0.25x-2.0x based on cross-system conviction                  ║
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
║  ├── titan/                   Stock breakout system                        ║
║  │   ├── runner.py            Scanner + execution                         ║
║  │   └── logs/                Trades, orders, heartbeat                   ║
║  │                                                                         ║
║  ├── apollo/                  Earnings drift system                        ║
║  │   ├── runner.py            ER scanner + drift trader                   ║
║  │   └── logs/                Scans, trades, heartbeat                    ║
║  │                                                                         ║
║  ├── hermes/                  Gap trading system                           ║
║  │   ├── runner.py            Gap detection + entry                       ║
║  │   └── logs/                Trades, heartbeat                           ║
║  │                                                                         ║
║  ├── ares/                    Monthly rotation (reporting-only)            ║
║  │   └── runner.py            Momentum-ranked ETF rotation                ║
║  │                                                                         ║
║  ├── helio/                   Fleet infrastructure                         ║
║  │   ├── fleet_monitor.py     Heartbeat watchdog + auto-restart           ║
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
║  │   │   ├── cascade/         Templates (data-validated), tracker        ║
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
║  │   ├── paper_monitor_status.py  Fleet snapshot                         ║
║  │   ├── dashboard.py         Web dashboard                              ║
║  │   ├── ROADMAP_PHASE7.md    Build roadmap                              ║
║  │   ├── ARCHITECTURE_DECISION.md  Control plane decision                ║
║  │   ├── PARALLEL_BUILD_PLAN.md    Burn-in build plan                    ║
║  │   └── WEEK_1_REVIEW_PLAN.md     Friday review playbook               ║
║  │                                                                         ║
║  └── forge/data/              Runtime databases (gitignored)             ║
║      ├── atlas.db             Events + impacts + predictions             ║
║      ├── themis.db            Congressional trades + signals             ║
║      └── *.json               Impact matrix, Bayesian state, etc.        ║
║                                                                            ║
╚══════════════════════════════════════════════════════════════════════════════╝
```
