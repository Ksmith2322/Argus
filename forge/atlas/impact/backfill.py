"""
Atlas Impact Backfill — Curate ~175 major macro events (2000-2026) and measure
their Cumulative Abnormal Return (CAR) across 25 ETFs over 4 time windows.

This is the core research foundation for Atlas: the impact measurements become
the basis for all cascade templates and regime scoring.

Usage:
    python -m forge.atlas.impact.backfill
"""

import json
import logging
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf
from scipy import stats as sp_stats

from forge.atlas.db.schema import init_db
from forge.atlas.db.queries import insert_event, insert_impact

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

ASSETS = [
    # Broad equity
    "SPY", "QQQ", "IWM",
    # Sectors
    "XLE", "XLF", "XLK", "XLV", "XLI", "XLU", "XLY", "XLP",
    # Bonds
    "TLT", "HYG", "LQD",
    # Commodities
    "GLD", "SLV", "USO",
    # FX proxies
    "UUP", "FXE", "FXY",
    # Emerging markets
    "EEM", "FXI", "EWJ", "EWZ",
    # Volatility
    "VXX",
]

BENCHMARK = "SPY"

WINDOWS = [
    ("CAR(0,1)", 0, 1),
    ("CAR(1,5)", 1, 5),
    ("CAR(1,20)", 1, 20),
    ("CAR(0,20)", 0, 20),
]

BETA_LOOKBACK = 252  # trading days before event for beta estimation

DATA_DIR = Path(__file__).resolve().parents[2] / "data"

# ──────────────────────────────────────────────────────────────────────────────
# Part 1 — Hardcoded Event Database (~175 events)
# ──────────────────────────────────────────────────────────────────────────────

EVENTS: list[dict] = [
    # =========================================================================
    # FOMC DECISIONS (~40) — focus on surprises, not routine holds
    # =========================================================================
    {"date": "2001-01-03", "event_type": "FOMC_DECISION", "event_category": "MONETARY",
     "severity": 0.9, "description": "Fed surprise inter-meeting 50bp cut to 6.0%"},
    {"date": "2001-04-18", "event_type": "FOMC_DECISION", "event_category": "MONETARY",
     "severity": 0.8, "description": "Fed surprise inter-meeting 50bp cut to 4.5%"},
    {"date": "2001-09-17", "event_type": "FOMC_DECISION", "event_category": "MONETARY",
     "severity": 0.9, "description": "Fed emergency 50bp cut after 9/11 to 3.0%"},
    {"date": "2001-11-06", "event_type": "FOMC_DECISION", "event_category": "MONETARY",
     "severity": 0.6, "description": "Fed cuts 50bp to 2.0% amid recession"},
    {"date": "2002-11-06", "event_type": "FOMC_DECISION", "event_category": "MONETARY",
     "severity": 0.7, "description": "Fed surprise 50bp cut to 1.25%"},
    {"date": "2003-06-25", "event_type": "FOMC_DECISION", "event_category": "MONETARY",
     "severity": 0.6, "description": "Fed cuts to 1.0% — lowest since 1958"},
    {"date": "2004-06-30", "event_type": "FOMC_DECISION", "event_category": "MONETARY",
     "severity": 0.5, "description": "Fed begins tightening cycle — first hike to 1.25%"},
    {"date": "2006-06-29", "event_type": "FOMC_DECISION", "event_category": "MONETARY",
     "severity": 0.5, "description": "Fed hikes to 5.25% — final hike of tightening cycle"},
    {"date": "2007-09-18", "event_type": "FOMC_DECISION", "event_category": "MONETARY",
     "severity": 0.8, "description": "Fed surprise 50bp cut to 4.75% — start of easing"},
    {"date": "2008-01-22", "event_type": "FOMC_DECISION", "event_category": "MONETARY",
     "severity": 0.9, "description": "Fed emergency inter-meeting 75bp cut to 3.5%"},
    {"date": "2008-03-18", "event_type": "FOMC_DECISION", "event_category": "MONETARY",
     "severity": 0.7, "description": "Fed cuts 75bp to 2.25% amid Bear Stearns crisis"},
    {"date": "2008-10-08", "event_type": "FOMC_DECISION", "event_category": "MONETARY",
     "severity": 0.9, "description": "Coordinated global central bank 50bp emergency cut"},
    {"date": "2008-10-29", "event_type": "FOMC_DECISION", "event_category": "MONETARY",
     "severity": 0.7, "description": "Fed cuts 50bp to 1.0%"},
    {"date": "2008-12-16", "event_type": "FOMC_DECISION", "event_category": "MONETARY",
     "severity": 0.9, "description": "Fed cuts to 0-0.25% range — zero lower bound reached"},
    {"date": "2010-11-03", "event_type": "QE_QT_CHANGE", "event_category": "MONETARY",
     "severity": 0.8, "description": "Fed announces QE2 — $600B Treasury purchases"},
    {"date": "2012-09-13", "event_type": "QE_QT_CHANGE", "event_category": "MONETARY",
     "severity": 0.8, "description": "Fed announces QE3 — open-ended $40B/month MBS"},
    {"date": "2013-05-22", "event_type": "FOMC_DECISION", "event_category": "MONETARY",
     "severity": 0.8, "description": "Bernanke hints at taper — Taper Tantrum begins"},
    {"date": "2013-12-18", "event_type": "FOMC_DECISION", "event_category": "MONETARY",
     "severity": 0.6, "description": "Fed announces QE3 taper to begin in January"},
    {"date": "2015-12-16", "event_type": "FOMC_DECISION", "event_category": "MONETARY",
     "severity": 0.7, "description": "Fed first rate hike in 9 years — 25bp to 0.25-0.50%"},
    {"date": "2017-06-14", "event_type": "QE_QT_CHANGE", "event_category": "MONETARY",
     "severity": 0.5, "description": "Fed announces balance sheet normalization plan (QT)"},
    {"date": "2018-12-19", "event_type": "FOMC_DECISION", "event_category": "MONETARY",
     "severity": 0.7, "description": "Fed hikes 25bp + hawkish dot plot — market rout"},
    {"date": "2019-01-30", "event_type": "FOMC_DECISION", "event_category": "MONETARY",
     "severity": 0.7, "description": "Fed pivot — removes 'further gradual increases' language"},
    {"date": "2019-07-31", "event_type": "FOMC_DECISION", "event_category": "MONETARY",
     "severity": 0.6, "description": "Fed first rate cut since 2008 — 25bp 'insurance cut'"},
    {"date": "2019-10-30", "event_type": "FOMC_DECISION", "event_category": "MONETARY",
     "severity": 0.5, "description": "Fed third consecutive 25bp cut to 1.50-1.75%"},
    {"date": "2020-03-03", "event_type": "FOMC_DECISION", "event_category": "MONETARY",
     "severity": 0.9, "description": "Fed emergency 50bp inter-meeting cut for COVID"},
    {"date": "2020-03-15", "event_type": "FOMC_DECISION", "event_category": "MONETARY",
     "severity": 1.0, "description": "Fed emergency 100bp cut to zero + $700B QE — Sunday announcement"},
    {"date": "2021-11-03", "event_type": "FOMC_DECISION", "event_category": "MONETARY",
     "severity": 0.5, "description": "Fed announces QE taper — $15B/month reduction"},
    {"date": "2022-01-26", "event_type": "FOMC_DECISION", "event_category": "MONETARY",
     "severity": 0.6, "description": "Fed hawkish hold — signals March liftoff imminent"},
    {"date": "2022-03-16", "event_type": "FOMC_DECISION", "event_category": "MONETARY",
     "severity": 0.6, "description": "Fed first hike of cycle — 25bp to 0.25-0.50%"},
    {"date": "2022-06-15", "event_type": "FOMC_DECISION", "event_category": "MONETARY",
     "severity": 0.8, "description": "Fed surprise 75bp hike — largest since 1994"},
    {"date": "2022-09-21", "event_type": "FOMC_DECISION", "event_category": "MONETARY",
     "severity": 0.7, "description": "Fed third consecutive 75bp hike + hawkish dot plot"},
    {"date": "2022-11-02", "event_type": "FOMC_DECISION", "event_category": "MONETARY",
     "severity": 0.6, "description": "Fed fourth 75bp hike — hints at slower pace ahead"},
    {"date": "2023-02-01", "event_type": "FOMC_DECISION", "event_category": "MONETARY",
     "severity": 0.5, "description": "Fed downshifts to 25bp hike — 4.50-4.75%"},
    {"date": "2023-06-14", "event_type": "FOMC_DECISION", "event_category": "MONETARY",
     "severity": 0.5, "description": "Fed hawkish skip — holds but signals more hikes"},
    {"date": "2023-07-26", "event_type": "FOMC_DECISION", "event_category": "MONETARY",
     "severity": 0.5, "description": "Fed hikes 25bp to 5.25-5.50% — final hike of cycle"},
    {"date": "2023-12-13", "event_type": "FOMC_DECISION", "event_category": "MONETARY",
     "severity": 0.7, "description": "Fed dovish pivot — dot plot signals 75bp of cuts in 2024"},
    {"date": "2024-09-18", "event_type": "FOMC_DECISION", "event_category": "MONETARY",
     "severity": 0.8, "description": "Fed begins easing cycle with 50bp cut to 4.75-5.00%"},
    {"date": "2024-12-18", "event_type": "FOMC_DECISION", "event_category": "MONETARY",
     "severity": 0.7, "description": "Fed cuts 25bp but hawkish dot plot — fewer 2025 cuts signaled"},
    {"date": "2025-03-19", "event_type": "FOMC_DECISION", "event_category": "MONETARY",
     "severity": 0.6, "description": "Fed holds amid tariff uncertainty — signals patience"},
    {"date": "2025-06-18", "event_type": "FOMC_DECISION", "event_category": "MONETARY",
     "severity": 0.6, "description": "Fed holds at 4.25-4.50% — cites tariff inflation risk"},

    # =========================================================================
    # OPEC EVENTS (~20)
    # =========================================================================
    {"date": "2001-09-26", "event_type": "OPEC_CUT", "event_category": "ENERGY",
     "severity": 0.6, "description": "OPEC cuts production 1M bpd post-9/11"},
    {"date": "2003-01-12", "event_type": "OPEC_CUT", "event_category": "ENERGY",
     "severity": 0.6, "description": "OPEC cuts 1.5M bpd ahead of Iraq invasion fears"},
    {"date": "2006-10-20", "event_type": "OPEC_CUT", "event_category": "ENERGY",
     "severity": 0.5, "description": "OPEC cuts 1.2M bpd to arrest price slide"},
    {"date": "2008-09-10", "event_type": "OPEC_CUT", "event_category": "ENERGY",
     "severity": 0.6, "description": "OPEC cuts 520K bpd as oil falls from $147 peak"},
    {"date": "2008-12-17", "event_type": "OPEC_CUT", "event_category": "ENERGY",
     "severity": 0.8, "description": "OPEC record 2.2M bpd cut amid financial crisis"},
    {"date": "2011-06-08", "event_type": "OPEC_INCREASE", "event_category": "ENERGY",
     "severity": 0.6, "description": "OPEC fails to agree on output increase — Saudi goes alone"},
    {"date": "2014-11-27", "event_type": "OIL_MOVE", "event_category": "ENERGY",
     "severity": 0.9, "description": "OPEC refuses to cut — oil price war begins, $30 crash"},
    {"date": "2016-09-28", "event_type": "OPEC_CUT", "event_category": "ENERGY",
     "severity": 0.8, "description": "OPEC Algiers Accord — surprise agreement to cut production"},
    {"date": "2016-11-30", "event_type": "OPEC_CUT", "event_category": "ENERGY",
     "severity": 0.7, "description": "OPEC Vienna deal — 1.2M bpd cut with Russia"},
    {"date": "2017-11-30", "event_type": "OPEC_CUT", "event_category": "ENERGY",
     "severity": 0.5, "description": "OPEC+ extends cuts through end of 2018"},
    {"date": "2018-06-22", "event_type": "OPEC_INCREASE", "event_category": "ENERGY",
     "severity": 0.6, "description": "OPEC+ agrees to increase output by ~1M bpd"},
    {"date": "2020-03-06", "event_type": "OIL_MOVE", "event_category": "ENERGY",
     "severity": 1.0, "description": "Saudi-Russia oil price war — Saudi slashes prices"},
    {"date": "2020-04-12", "event_type": "OPEC_CUT", "event_category": "ENERGY",
     "severity": 0.9, "description": "OPEC+ historic 9.7M bpd cut agreement"},
    {"date": "2020-04-20", "event_type": "OIL_MOVE", "event_category": "ENERGY",
     "severity": 1.0, "description": "WTI crude goes negative — -$37.63/barrel"},
    {"date": "2022-10-05", "event_type": "OPEC_CUT", "event_category": "ENERGY",
     "severity": 0.7, "description": "OPEC+ surprise 2M bpd cut despite US pressure"},
    {"date": "2023-04-02", "event_type": "OPEC_CUT", "event_category": "ENERGY",
     "severity": 0.7, "description": "OPEC+ surprise voluntary 1.16M bpd cut"},
    {"date": "2023-11-30", "event_type": "OPEC_CUT", "event_category": "ENERGY",
     "severity": 0.5, "description": "OPEC+ extends and deepens cuts into 2024"},
    {"date": "2024-06-02", "event_type": "OPEC_INCREASE", "event_category": "ENERGY",
     "severity": 0.6, "description": "OPEC+ begins gradual output increase plan"},
    {"date": "2025-01-15", "event_type": "OPEC_CUT", "event_category": "ENERGY",
     "severity": 0.6, "description": "OPEC+ delays planned output increases amid weak demand"},
    {"date": "2025-04-03", "event_type": "OPEC_INCREASE", "event_category": "ENERGY",
     "severity": 0.7, "description": "OPEC+ surprise output increase — 411K bpd in May"},

    # =========================================================================
    # TARIFF / TRADE EVENTS (~25)
    # =========================================================================
    {"date": "2002-03-05", "event_type": "TARIFF_ANNOUNCE", "event_category": "TRADE",
     "severity": 0.5, "description": "Bush imposes steel tariffs up to 30%"},
    {"date": "2018-03-01", "event_type": "TARIFF_ANNOUNCE", "event_category": "TRADE",
     "severity": 0.7, "description": "Trump announces 25% steel / 10% aluminum tariffs"},
    {"date": "2018-03-22", "event_type": "TARIFF_ANNOUNCE", "event_category": "TRADE",
     "severity": 0.8, "description": "Trump signs memo for $50B China tariffs — Section 301"},
    {"date": "2018-06-15", "event_type": "TARIFF_ANNOUNCE", "event_category": "TRADE",
     "severity": 0.7, "description": "US announces 25% tariffs on $50B of Chinese goods"},
    {"date": "2018-07-06", "event_type": "TARIFF_ANNOUNCE", "event_category": "TRADE",
     "severity": 0.7, "description": "US-China tariffs take effect — $34B each side"},
    {"date": "2018-09-17", "event_type": "TARIFF_ANNOUNCE", "event_category": "TRADE",
     "severity": 0.7, "description": "US announces 10% tariff on $200B Chinese goods"},
    {"date": "2018-12-01", "event_type": "TRADE_DEESCALATION", "event_category": "TRADE",
     "severity": 0.6, "description": "Trump-Xi G20 dinner truce — tariff hike delayed 90 days"},
    {"date": "2019-05-05", "event_type": "TARIFF_ANNOUNCE", "event_category": "TRADE",
     "severity": 0.8, "description": "Trump tweets tariff escalation to 25% on $200B"},
    {"date": "2019-08-01", "event_type": "TARIFF_ANNOUNCE", "event_category": "TRADE",
     "severity": 0.8, "description": "Trump announces 10% tariff on remaining $300B Chinese goods"},
    {"date": "2019-08-05", "event_type": "TARIFF_ANNOUNCE", "event_category": "TRADE",
     "severity": 0.7, "description": "China lets yuan break 7.0 — currency war fears"},
    {"date": "2019-08-23", "event_type": "TARIFF_ANNOUNCE", "event_category": "TRADE",
     "severity": 0.7, "description": "China retaliates with tariffs on $75B US goods"},
    {"date": "2019-10-11", "event_type": "TRADE_DEESCALATION", "event_category": "TRADE",
     "severity": 0.7, "description": "US-China Phase 1 deal framework announced"},
    {"date": "2020-01-15", "event_type": "TRADE_DEESCALATION", "event_category": "TRADE",
     "severity": 0.6, "description": "US-China Phase 1 trade deal signed"},
    {"date": "2022-08-09", "event_type": "EXPORT_CONTROLS", "event_category": "TRADE",
     "severity": 0.7, "description": "CHIPS Act signed — semiconductor export controls to China"},
    {"date": "2022-10-07", "event_type": "EXPORT_CONTROLS", "event_category": "TRADE",
     "severity": 0.8, "description": "US sweeping chip export controls on China"},
    {"date": "2025-02-01", "event_type": "TARIFF_ANNOUNCE", "event_category": "TRADE",
     "severity": 0.8, "description": "Trump 25% tariff on Canada/Mexico, 10% on China"},
    {"date": "2025-02-04", "event_type": "TRADE_DEESCALATION", "event_category": "TRADE",
     "severity": 0.7, "description": "US delays Canada/Mexico tariffs by 1 month after calls"},
    {"date": "2025-03-04", "event_type": "TARIFF_ANNOUNCE", "event_category": "TRADE",
     "severity": 0.8, "description": "Trump 25% tariffs on Canada/Mexico take effect + China to 20%"},
    {"date": "2025-03-12", "event_type": "TARIFF_ANNOUNCE", "event_category": "TRADE",
     "severity": 0.6, "description": "Trump 25% tariffs on all steel and aluminum imports"},
    {"date": "2025-04-02", "event_type": "TARIFF_ANNOUNCE", "event_category": "TRADE",
     "severity": 1.0, "description": "Liberation Day — sweeping reciprocal tariffs on all countries"},
    {"date": "2025-04-03", "event_type": "TARIFF_ANNOUNCE", "event_category": "TRADE",
     "severity": 0.9, "description": "Markets crash on Liberation Day tariff details — 10% baseline + higher"},
    {"date": "2025-04-09", "event_type": "TRADE_DEESCALATION", "event_category": "TRADE",
     "severity": 0.9, "description": "Trump pauses reciprocal tariffs 90 days (except China) — market rally"},
    {"date": "2025-04-10", "event_type": "TARIFF_ANNOUNCE", "event_category": "TRADE",
     "severity": 0.8, "description": "US-China tariffs escalate to 145% on Chinese goods"},
    {"date": "2016-06-24", "event_type": "TRADE_DEESCALATION", "event_category": "TRADE",
     "severity": 0.5, "description": "EU-Canada CETA trade deal finalized"},

    # =========================================================================
    # GEOPOLITICAL (~20)
    # =========================================================================
    {"date": "2001-09-11", "event_type": "TERROR_ATTACK", "event_category": "GEOPOLITICAL",
     "severity": 1.0, "description": "September 11 attacks — World Trade Center and Pentagon"},
    {"date": "2003-03-20", "event_type": "MILITARY_CONFLICT", "event_category": "GEOPOLITICAL",
     "severity": 0.9, "description": "US invasion of Iraq begins — Operation Iraqi Freedom"},
    {"date": "2006-07-12", "event_type": "MILITARY_CONFLICT", "event_category": "GEOPOLITICAL",
     "severity": 0.6, "description": "Israel-Lebanon war begins — Hezbollah cross-border raid"},
    {"date": "2008-08-08", "event_type": "MILITARY_CONFLICT", "event_category": "GEOPOLITICAL",
     "severity": 0.6, "description": "Russia-Georgia war begins"},
    {"date": "2011-03-19", "event_type": "MILITARY_CONFLICT", "event_category": "GEOPOLITICAL",
     "severity": 0.6, "description": "NATO military intervention in Libya begins"},
    {"date": "2014-03-01", "event_type": "MILITARY_CONFLICT", "event_category": "GEOPOLITICAL",
     "severity": 0.7, "description": "Russia annexes Crimea — troops enter Ukraine"},
    {"date": "2014-07-17", "event_type": "MILITARY_CONFLICT", "event_category": "GEOPOLITICAL",
     "severity": 0.6, "description": "MH17 shot down over eastern Ukraine"},
    {"date": "2017-08-08", "event_type": "NUCLEAR_THREAT", "event_category": "GEOPOLITICAL",
     "severity": 0.7, "description": "Trump 'fire and fury' threat to North Korea"},
    {"date": "2017-09-03", "event_type": "NUCLEAR_THREAT", "event_category": "GEOPOLITICAL",
     "severity": 0.7, "description": "North Korea sixth nuclear test — hydrogen bomb"},
    {"date": "2020-01-03", "event_type": "MILITARY_CONFLICT", "event_category": "GEOPOLITICAL",
     "severity": 0.8, "description": "US drone strike kills Iran's Soleimani"},
    {"date": "2022-02-24", "event_type": "MILITARY_CONFLICT", "event_category": "GEOPOLITICAL",
     "severity": 1.0, "description": "Russia invades Ukraine — full-scale war begins"},
    {"date": "2022-09-26", "event_type": "MILITARY_CONFLICT", "event_category": "GEOPOLITICAL",
     "severity": 0.6, "description": "Nord Stream pipeline sabotage — gas supply disruption"},
    {"date": "2023-10-07", "event_type": "MILITARY_CONFLICT", "event_category": "GEOPOLITICAL",
     "severity": 0.9, "description": "Hamas attack on Israel — Middle East crisis escalation"},
    {"date": "2023-10-27", "event_type": "MILITARY_CONFLICT", "event_category": "GEOPOLITICAL",
     "severity": 0.7, "description": "Israel ground operation in Gaza begins"},
    {"date": "2024-04-13", "event_type": "MILITARY_CONFLICT", "event_category": "GEOPOLITICAL",
     "severity": 0.8, "description": "Iran launches drone/missile attack on Israel"},
    {"date": "2024-10-01", "event_type": "MILITARY_CONFLICT", "event_category": "GEOPOLITICAL",
     "severity": 0.7, "description": "Iran launches 180 ballistic missiles at Israel"},
    {"date": "2025-01-20", "event_type": "DEESCALATION", "event_category": "GEOPOLITICAL",
     "severity": 0.6, "description": "Israel-Hamas ceasefire takes effect in Gaza"},
    {"date": "2025-03-15", "event_type": "MILITARY_CONFLICT", "event_category": "GEOPOLITICAL",
     "severity": 0.7, "description": "Israel resumes Gaza military operations — ceasefire collapses"},
    {"date": "2022-03-02", "event_type": "SANCTIONS", "event_category": "TRADE",
     "severity": 0.8, "description": "Western nations cut Russia from SWIFT banking system"},
    {"date": "2025-02-18", "event_type": "DEESCALATION", "event_category": "GEOPOLITICAL",
     "severity": 0.5, "description": "US-Russia talks on Ukraine framework — market cautiously optimistic"},

    # =========================================================================
    # ECONOMIC DATA SURPRISES (~15)
    # =========================================================================
    {"date": "2008-09-05", "event_type": "NFP_RELEASE", "event_category": "ECONOMIC_DATA",
     "severity": 0.7, "description": "NFP shows -84K jobs — worst in 5 years"},
    {"date": "2009-01-09", "event_type": "NFP_RELEASE", "event_category": "ECONOMIC_DATA",
     "severity": 0.8, "description": "NFP shows -524K — massive job losses accelerate"},
    {"date": "2013-09-06", "event_type": "NFP_RELEASE", "event_category": "ECONOMIC_DATA",
     "severity": 0.5, "description": "NFP misses big at +169K vs +180K expected"},
    {"date": "2016-06-03", "event_type": "NFP_RELEASE", "event_category": "ECONOMIC_DATA",
     "severity": 0.7, "description": "NFP disaster +38K vs +160K expected"},
    {"date": "2020-05-08", "event_type": "NFP_RELEASE", "event_category": "ECONOMIC_DATA",
     "severity": 0.9, "description": "NFP shows -20.5M jobs — worst single-month loss ever"},
    {"date": "2021-06-10", "event_type": "CPI_RELEASE", "event_category": "ECONOMIC_DATA",
     "severity": 0.7, "description": "CPI surges 5.0% YoY — 13-year high, transitory debate"},
    {"date": "2021-11-10", "event_type": "CPI_RELEASE", "event_category": "ECONOMIC_DATA",
     "severity": 0.8, "description": "CPI hits 6.2% YoY — 31-year high, transitory narrative ends"},
    {"date": "2022-06-10", "event_type": "CPI_RELEASE", "event_category": "ECONOMIC_DATA",
     "severity": 0.9, "description": "CPI hits 8.6% YoY — 40-year high, worse than expected"},
    {"date": "2022-07-13", "event_type": "CPI_RELEASE", "event_category": "ECONOMIC_DATA",
     "severity": 0.8, "description": "CPI peaks at 9.1% YoY — highest since Nov 1981"},
    {"date": "2022-11-10", "event_type": "CPI_RELEASE", "event_category": "ECONOMIC_DATA",
     "severity": 0.7, "description": "CPI drops to 7.7% — cooler than expected, market rally"},
    {"date": "2023-01-12", "event_type": "CPI_RELEASE", "event_category": "ECONOMIC_DATA",
     "severity": 0.5, "description": "CPI cools to 6.5% YoY — disinflation trend confirmed"},
    {"date": "2024-01-11", "event_type": "CPI_RELEASE", "event_category": "ECONOMIC_DATA",
     "severity": 0.5, "description": "CPI comes in at 3.4% vs 3.2% expected — sticky inflation"},
    {"date": "2024-09-06", "event_type": "NFP_RELEASE", "event_category": "ECONOMIC_DATA",
     "severity": 0.6, "description": "NFP +142K vs +165K expected — weakening labor market"},
    {"date": "2025-03-07", "event_type": "NFP_RELEASE", "event_category": "ECONOMIC_DATA",
     "severity": 0.6, "description": "NFP weaker than expected — recession fears grow amid tariffs"},
    {"date": "2025-04-10", "event_type": "CPI_RELEASE", "event_category": "ECONOMIC_DATA",
     "severity": 0.6, "description": "CPI cooler than expected at 2.4% — but tariff effects pending"},

    # =========================================================================
    # BANKING / FINANCIAL CRISES (~10)
    # =========================================================================
    {"date": "2008-03-14", "event_type": "BANK_FAILURE", "event_category": "FINANCIAL",
     "severity": 0.9, "description": "Bear Stearns bailout — JPMorgan rescue at $2/share"},
    {"date": "2008-09-15", "event_type": "BANK_FAILURE", "event_category": "FINANCIAL",
     "severity": 1.0, "description": "Lehman Brothers files bankruptcy — global financial crisis"},
    {"date": "2008-09-16", "event_type": "CREDIT_CRISIS", "event_category": "FINANCIAL",
     "severity": 0.9, "description": "AIG $85B government bailout — systemic risk"},
    {"date": "2010-05-06", "event_type": "CREDIT_CRISIS", "event_category": "FINANCIAL",
     "severity": 0.8, "description": "Flash Crash — Dow drops 1000 points in minutes"},
    {"date": "2010-05-02", "event_type": "CREDIT_CRISIS", "event_category": "FINANCIAL",
     "severity": 0.8, "description": "Greece bailout — European sovereign debt crisis erupts"},
    {"date": "2011-08-05", "event_type": "CREDIT_DOWNGRADE", "event_category": "FINANCIAL",
     "severity": 0.8, "description": "S&P downgrades US credit rating from AAA to AA+"},
    {"date": "2012-06-09", "event_type": "CREDIT_CRISIS", "event_category": "FINANCIAL",
     "severity": 0.7, "description": "Spain requests bank bailout — eurozone crisis deepens"},
    {"date": "2023-03-10", "event_type": "BANK_FAILURE", "event_category": "FINANCIAL",
     "severity": 0.9, "description": "Silicon Valley Bank collapses — largest failure since 2008"},
    {"date": "2023-03-15", "event_type": "BANK_FAILURE", "event_category": "FINANCIAL",
     "severity": 0.8, "description": "Credit Suisse crisis — emergency UBS takeover announced"},
    {"date": "2023-05-01", "event_type": "BANK_FAILURE", "event_category": "FINANCIAL",
     "severity": 0.7, "description": "First Republic Bank seized by FDIC — sold to JPMorgan"},

    # =========================================================================
    # ELECTIONS / POLITICAL (~10)
    # =========================================================================
    {"date": "2008-11-04", "event_type": "ELECTION_RESULT", "event_category": "POLITICAL",
     "severity": 0.5, "description": "Obama elected president amid financial crisis"},
    {"date": "2010-11-02", "event_type": "ELECTION_RESULT", "event_category": "POLITICAL",
     "severity": 0.4, "description": "Republicans win House in Tea Party wave midterms"},
    {"date": "2013-10-01", "event_type": "FISCAL_CRISIS", "event_category": "POLITICAL",
     "severity": 0.6, "description": "US government shutdown begins — 16 days"},
    {"date": "2016-06-23", "event_type": "ELECTION_RESULT", "event_category": "POLITICAL",
     "severity": 0.9, "description": "Brexit referendum — UK votes to leave EU"},
    {"date": "2016-11-08", "event_type": "ELECTION_RESULT", "event_category": "POLITICAL",
     "severity": 0.8, "description": "Trump elected president — surprise market rally"},
    {"date": "2020-11-03", "event_type": "ELECTION_RESULT", "event_category": "POLITICAL",
     "severity": 0.6, "description": "Biden wins presidency — contested transition period"},
    {"date": "2021-01-06", "event_type": "ELECTION_RESULT", "event_category": "POLITICAL",
     "severity": 0.6, "description": "US Capitol riot — but market impact muted"},
    {"date": "2022-11-08", "event_type": "ELECTION_RESULT", "event_category": "POLITICAL",
     "severity": 0.5, "description": "US midterms — no red wave, split government continues"},
    {"date": "2024-11-05", "event_type": "ELECTION_RESULT", "event_category": "POLITICAL",
     "severity": 0.7, "description": "Trump wins 2024 election — market rally on tax/dereg hopes"},
    {"date": "2025-01-20", "event_type": "ELECTION_RESULT", "event_category": "POLITICAL",
     "severity": 0.5, "description": "Trump inaugurated — executive orders on Day 1"},

    # =========================================================================
    # PANDEMIC / BLACK SWAN (~5)
    # =========================================================================
    {"date": "2011-03-11", "event_type": "NATURAL_DISASTER", "event_category": "BLACK_SWAN",
     "severity": 0.8, "description": "Fukushima earthquake/tsunami — nuclear disaster in Japan"},
    {"date": "2020-01-20", "event_type": "PANDEMIC", "event_category": "BLACK_SWAN",
     "severity": 0.6, "description": "First confirmed US COVID case — pandemic fears emerge"},
    {"date": "2020-02-24", "event_type": "PANDEMIC", "event_category": "BLACK_SWAN",
     "severity": 0.9, "description": "COVID market crash begins — Italy outbreak triggers selloff"},
    {"date": "2020-03-11", "event_type": "PANDEMIC", "event_category": "BLACK_SWAN",
     "severity": 1.0, "description": "WHO declares COVID-19 global pandemic"},
    {"date": "2020-03-23", "event_type": "PANDEMIC", "event_category": "BLACK_SWAN",
     "severity": 0.9, "description": "COVID market bottom — SPY hits 2191, massive Fed intervention"},

    # =========================================================================
    # CURRENCY CRISES (~5)
    # =========================================================================
    {"date": "2010-06-07", "event_type": "CREDIT_CRISIS", "event_category": "FINANCIAL",
     "severity": 0.6, "description": "Hungarian debt concerns — euro drops sharply, EM wobble"},
    {"date": "2015-01-15", "event_type": "CREDIT_CRISIS", "event_category": "FINANCIAL",
     "severity": 0.9, "description": "Swiss franc unpegged from euro — CHF surges 30%"},
    {"date": "2015-08-11", "event_type": "CREDIT_CRISIS", "event_category": "FINANCIAL",
     "severity": 0.8, "description": "China devalues yuan — global market contagion fears"},
    {"date": "2018-08-10", "event_type": "CREDIT_CRISIS", "event_category": "FINANCIAL",
     "severity": 0.6, "description": "Turkish lira crisis — EM contagion fears spread"},
    {"date": "2022-09-23", "event_type": "CREDIT_CRISIS", "event_category": "FINANCIAL",
     "severity": 0.7, "description": "UK gilt market crisis — BoE emergency intervention"},

    # =========================================================================
    # OIL SUPPLY DISRUPTIONS (~5)
    # =========================================================================
    {"date": "2005-08-29", "event_type": "OIL_SUPPLY_DISRUPTION", "event_category": "ENERGY",
     "severity": 0.8, "description": "Hurricane Katrina devastates Gulf oil infrastructure"},
    {"date": "2011-02-22", "event_type": "OIL_SUPPLY_DISRUPTION", "event_category": "ENERGY",
     "severity": 0.7, "description": "Libyan civil war — oil production collapses"},
    {"date": "2019-09-14", "event_type": "OIL_SUPPLY_DISRUPTION", "event_category": "ENERGY",
     "severity": 0.9, "description": "Saudi Aramco attack — 5.7M bpd knocked offline"},
    {"date": "2024-01-12", "event_type": "OIL_SUPPLY_DISRUPTION", "event_category": "ENERGY",
     "severity": 0.6, "description": "Houthi Red Sea attacks disrupt shipping — oil supply fears"},
    {"date": "2025-03-22", "event_type": "OIL_SUPPLY_DISRUPTION", "event_category": "ENERGY",
     "severity": 0.7, "description": "Strait of Hormuz tensions escalate — Iran naval exercises"},
]

# ──────────────────────────────────────────────────────────────────────────────
# Part 2 — Price Data & Impact Measurement
# ──────────────────────────────────────────────────────────────────────────────

def download_all_prices() -> dict[str, pd.DataFrame]:
    """Download daily adjusted-close prices for all assets + benchmark.
    Returns {ticker: DataFrame with 'Close' column indexed by date}.
    """
    tickers = sorted(set(ASSETS + [BENCHMARK]))
    log.info("Downloading price data for %d tickers ...", len(tickers))

    cache: dict[str, pd.DataFrame] = {}
    for ticker in tickers:
        try:
            log.info("  Fetching %s ...", ticker)
            df = yf.download(ticker, start="1999-01-01", end="2026-04-12",
                             auto_adjust=True, progress=False)
            if df.empty:
                log.warning("  %s returned empty data — skipping", ticker)
                continue
            # Flatten MultiIndex columns if present (yfinance sometimes returns them)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df[["Close"]].copy()
            df.index = pd.to_datetime(df.index).tz_localize(None)
            df = df.sort_index()
            cache[ticker] = df
            log.info("    %s: %d rows (%s to %s)", ticker, len(df),
                     df.index[0].date(), df.index[-1].date())
        except Exception as exc:
            log.warning("  %s download failed: %s", ticker, exc)
    return cache


def find_trading_day(prices: pd.DataFrame, target_date: pd.Timestamp,
                     direction: str = "forward", max_offset: int = 5) -> Optional[int]:
    """Find the positional index of the nearest trading day to target_date.
    direction='forward' looks ahead; 'backward' looks behind.
    Returns integer positional index into prices, or None.
    """
    idx = prices.index
    if target_date in idx:
        return idx.get_loc(target_date)

    if direction == "forward":
        mask = idx >= target_date
        if mask.any():
            return idx.get_loc(idx[mask][0])
    else:
        mask = idx <= target_date
        if mask.any():
            return idx.get_loc(idx[mask][-1])
    return None


def compute_return(prices: pd.DataFrame, start_idx: int, end_idx: int) -> Optional[float]:
    """Compute simple return from positional index start_idx to end_idx."""
    if start_idx < 0 or end_idx < 0:
        return None
    if start_idx >= len(prices) or end_idx >= len(prices):
        return None
    p0 = prices.iloc[start_idx]["Close"]
    p1 = prices.iloc[end_idx]["Close"]
    if p0 == 0 or pd.isna(p0) or pd.isna(p1):
        return None
    return float((p1 - p0) / p0)


def estimate_beta(asset_prices: pd.DataFrame, bench_prices: pd.DataFrame,
                  event_idx_asset: int, event_idx_bench: int) -> float:
    """Estimate beta from the BETA_LOOKBACK days before event.
    Returns 1.0 if insufficient data.
    """
    start_a = max(0, event_idx_asset - BETA_LOOKBACK)
    start_b = max(0, event_idx_bench - BETA_LOOKBACK)
    if event_idx_asset - start_a < 60 or event_idx_bench - start_b < 60:
        return 1.0  # insufficient data

    asset_rets = asset_prices.iloc[start_a:event_idx_asset]["Close"].pct_change().dropna()
    bench_rets = bench_prices.iloc[start_b:event_idx_bench]["Close"].pct_change().dropna()

    # Align by date
    common = asset_rets.index.intersection(bench_rets.index)
    if len(common) < 60:
        return 1.0

    a = asset_rets.loc[common].values
    b = bench_rets.loc[common].values

    cov = np.cov(a, b)
    var_bench = cov[1, 1]
    if var_bench == 0:
        return 1.0
    beta = cov[0, 1] / var_bench
    return float(np.clip(beta, -3.0, 5.0))


def measure_event_impacts(
    event: dict,
    price_cache: dict[str, pd.DataFrame],
) -> list[dict]:
    """Compute CAR for one event across all assets and windows.
    Returns list of impact dicts.
    """
    event_date = pd.Timestamp(event["date"])
    bench_prices = price_cache.get(BENCHMARK)
    if bench_prices is None:
        return []

    bench_event_idx = find_trading_day(bench_prices, event_date, "forward")
    if bench_event_idx is None:
        return []

    results = []
    for asset in ASSETS:
        if asset not in price_cache:
            continue
        ap = price_cache[asset]

        # Find event day in this asset's history
        asset_event_idx = find_trading_day(ap, event_date, "forward")
        if asset_event_idx is None:
            continue

        # Check that event date is close enough (within 5 days)
        actual_event_date = ap.index[asset_event_idx]
        if abs((actual_event_date - event_date).days) > 5:
            continue

        # Estimate beta
        beta = estimate_beta(ap, bench_prices, asset_event_idx, bench_event_idx)

        for label, w_start, w_end in WINDOWS:
            # Asset return
            idx_start = asset_event_idx + w_start
            idx_end = asset_event_idx + w_end
            raw_ret = compute_return(ap, idx_start, idx_end)
            if raw_ret is None:
                continue

            # Benchmark return
            b_idx_start = bench_event_idx + w_start
            b_idx_end = bench_event_idx + w_end
            bench_ret = compute_return(bench_prices, b_idx_start, b_idx_end)
            if bench_ret is None:
                continue

            # Abnormal return
            abnormal = raw_ret - beta * bench_ret

            results.append({
                "asset": asset,
                "window_label": label,
                "window_start": w_start,
                "window_end": w_end,
                "raw_return": round(raw_ret, 6),
                "abnormal_return": round(abnormal, 6),
                "benchmark_return": round(bench_ret, 6),
                "beta": round(beta, 4),
            })
    return results


# ──────────────────────────────────────────────────────────────────────────────
# Part 3 — Build Impact Matrix (aggregated stats)
# ──────────────────────────────────────────────────────────────────────────────

def build_impact_matrix(all_measurements: list[dict]) -> dict:
    """
    Aggregate measurements into:
      {event_type: {asset: {window_label: {mean, std, n, t_stat, p_value}}}}
    """
    from collections import defaultdict

    # Group: event_type -> asset -> window_label -> list of abnormal returns
    groups: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))

    for m in all_measurements:
        groups[m["event_type"]][m["asset"]][m["window_label"]].append(m["abnormal_return"])

    matrix: dict = {}
    for etype, assets in sorted(groups.items()):
        matrix[etype] = {}
        for asset, windows in sorted(assets.items()):
            matrix[etype][asset] = {}
            for wlabel, values in sorted(windows.items()):
                arr = np.array(values)
                n = len(arr)
                mean = float(np.mean(arr))
                std = float(np.std(arr, ddof=1)) if n > 1 else 0.0

                if n > 1 and std > 0:
                    t_stat = mean / (std / math.sqrt(n))
                    p_value = float(2 * sp_stats.t.sf(abs(t_stat), df=n - 1))
                else:
                    t_stat = 0.0
                    p_value = 1.0

                matrix[etype][asset][wlabel] = {
                    "mean": round(mean, 6),
                    "std": round(std, 6),
                    "n": n,
                    "t_stat": round(t_stat, 4),
                    "p_value": round(p_value, 4),
                }
    return matrix


def print_significant_pairs(matrix: dict, p_threshold: float = 0.10) -> None:
    """Print event-asset pairs with statistically significant abnormal returns."""
    sig_count = 0
    print("\n" + "=" * 90)
    print("SIGNIFICANT EVENT-ASSET PAIRS (p < {:.2f})".format(p_threshold))
    print("=" * 90)
    print(f"{'Event Type':<28} {'Asset':<6} {'Window':<12} {'Mean CAR':>10} "
          f"{'Std':>10} {'N':>4} {'t-stat':>8} {'p-value':>8}")
    print("-" * 90)

    for etype in sorted(matrix):
        for asset in sorted(matrix[etype]):
            for wlabel in sorted(matrix[etype][asset]):
                stats = matrix[etype][asset][wlabel]
                if stats["p_value"] < p_threshold and stats["n"] >= 3:
                    sig_count += 1
                    print(f"{etype:<28} {asset:<6} {wlabel:<12} "
                          f"{stats['mean']:>10.4%} {stats['std']:>10.4%} "
                          f"{stats['n']:>4d} {stats['t_stat']:>8.2f} "
                          f"{stats['p_value']:>8.4f}")

    print("-" * 90)
    print(f"Total significant pairs: {sig_count}")
    print("=" * 90)


# ──────────────────────────────────────────────────────────────────────────────
# Part 4 — Store Results
# ──────────────────────────────────────────────────────────────────────────────

def store_results(
    events_with_ids: list[tuple[dict, str, list[dict]]],
    matrix: dict,
) -> None:
    """Insert events and impacts into SQLite, save matrix to JSON."""
    conn = init_db()
    now_iso = datetime.now(timezone.utc).isoformat()

    inserted_events = 0
    inserted_impacts = 0

    for event, event_id, impacts in events_with_ids:
        # Insert event
        eid = insert_event(conn, {
            "event_id": event_id,
            "timestamp": f"{event['date']}T00:00:00+00:00",
            "detected_at": now_iso,
            "event_type": event["event_type"],
            "event_category": event["event_category"],
            "severity": event["severity"],
            "title": event["description"],
            "description": event["description"],
            "source": "backfill",
        })
        inserted_events += 1

        # Insert impacts
        for imp in impacts:
            insert_impact(
                conn,
                event_id=eid,
                asset=imp["asset"],
                window_label=imp["window_label"],
                window_start=imp["window_start"],
                window_end=imp["window_end"],
                raw_return=imp["raw_return"],
                abnormal_return=imp["abnormal_return"],
                benchmark_return=imp["benchmark_return"],
            )
            inserted_impacts += 1

    conn.close()
    log.info("Inserted %d events and %d impact rows into atlas.db", inserted_events, inserted_impacts)

    # Save matrix as JSON
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    matrix_path = DATA_DIR / "atlas_impact_matrix.json"
    with open(matrix_path, "w") as f:
        json.dump(matrix, f, indent=2)
    log.info("Impact matrix saved to %s", matrix_path)


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 70)
    print("ATLAS IMPACT BACKFILL")
    print(f"Events: {len(EVENTS)} | Assets: {len(ASSETS)} | Windows: {len(WINDOWS)}")
    print("=" * 70)

    # Validate event dates
    for e in EVENTS:
        try:
            datetime.strptime(e["date"], "%Y-%m-%d")
        except ValueError:
            log.error("Invalid date in event: %s", e)
            sys.exit(1)

    # Print category breakdown
    from collections import Counter
    cat_counts = Counter(e["event_category"] for e in EVENTS)
    type_counts = Counter(e["event_type"] for e in EVENTS)
    print("\nEvent categories:")
    for cat, cnt in sorted(cat_counts.items(), key=lambda x: -x[1]):
        print(f"  {cat:<25} {cnt:>3}")
    print(f"\nUnique event types: {len(type_counts)}")
    print()

    # Download prices
    price_cache = download_all_prices()
    if BENCHMARK not in price_cache:
        log.error("Failed to download benchmark (%s) — aborting", BENCHMARK)
        sys.exit(1)
    print(f"\nSuccessfully loaded {len(price_cache)} tickers\n")

    # Measure impacts
    all_measurements: list[dict] = []     # flat list with event_type added
    events_with_ids: list[tuple[dict, str, list[dict]]] = []
    import uuid

    total = len(EVENTS)
    for i, event in enumerate(EVENTS, 1):
        if i % 25 == 0 or i == total:
            log.info("Processing event %d/%d: %s", i, total, event["description"][:60])

        impacts = measure_event_impacts(event, price_cache)
        event_id = str(uuid.uuid4())
        events_with_ids.append((event, event_id, impacts))

        for imp in impacts:
            imp["event_type"] = event["event_type"]
            imp["event_category"] = event["event_category"]
            all_measurements.append(imp)

    print(f"\nTotal impact measurements: {len(all_measurements)}")

    # Build impact matrix
    matrix = build_impact_matrix(all_measurements)

    # Print significant pairs
    print_significant_pairs(matrix, p_threshold=0.10)

    # Print summary by event type
    print("\n" + "=" * 70)
    print("SUMMARY BY EVENT TYPE")
    print("=" * 70)
    print(f"{'Event Type':<28} {'Events':>7} {'Measurements':>14} {'Sig Pairs':>10}")
    print("-" * 70)

    for etype in sorted(matrix):
        n_events = sum(1 for e in EVENTS if e["event_type"] == etype)
        n_meas = sum(
            stats["n"]
            for asset in matrix[etype].values()
            for stats in asset.values()
        )
        n_sig = sum(
            1
            for asset in matrix[etype].values()
            for stats in asset.values()
            if stats["p_value"] < 0.10 and stats["n"] >= 3
        )
        print(f"{etype:<28} {n_events:>7} {n_meas:>14} {n_sig:>10}")

    print("=" * 70)

    # Store results
    store_results(events_with_ids, matrix)

    print("\nBackfill complete.")
    print(f"  Events: {len(EVENTS)}")
    print(f"  Impact measurements: {len(all_measurements)}")
    print(f"  Impact matrix saved to: {DATA_DIR / 'atlas_impact_matrix.json'}")
    print(f"  Database: {DATA_DIR / 'atlas.db'}")


if __name__ == "__main__":
    main()
