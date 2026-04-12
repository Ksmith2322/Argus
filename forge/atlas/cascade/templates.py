"""
Cascade template definitions mapping event types to expected asset impacts by wave.

Each template maps an event type to a list of expected impacts:
  - asset: ticker symbol
  - wave: cascade wave number (1 = immediate, 2 = secondary, 3 = tertiary)
  - direction: +1 or -1 (expected move direction)
  - expected_car_5d: expected cumulative abnormal return over 5 days (baseline)
  - confidence: "high", "medium", or "low"
  - lag_days: expected days after event before this asset reacts
  - description: rationale for the expected move
  - data_validated: True if backed by statistically significant backfill data (p<0.05, N>=5)
  - n: sample size (for validated entries)
  - p: p-value (for validated entries)

Direction conventions:
  - FOMC_DECISION: directions assume DOVISH surprise. Flip all for HAWKISH.
  - CPI_RELEASE: directions assume HOT print. Flip all for COOL.
  - NFP_RELEASE: directions assume STRONG print. Flip all for WEAK.
  - ELECTION_RESULT: directions assume pro-business / continuity outcome.

Data-validated entries sourced from 154-event macro backfill across 25 ETFs (2026-04).
"""

from __future__ import annotations

TEMPLATES: dict[str, list[dict]] = {
    # ──────────────────────────────────────────────────────────────────────
    # FOMC_DECISION — Dovish surprise baseline (flip for hawkish)
    # Data: XLF -1.30% over 20d (N=37, p=0.022) is the strongest signal.
    #       XLE -0.74% on day 1 (N=37, p=0.049).
    # ──────────────────────────────────────────────────────────────────────
    "FOMC_DECISION": [
        # Wave 1: Rates and FX react immediately
        {"asset": "TLT", "wave": 1, "direction": 1, "expected_car_5d": 0.015, "confidence": "medium", "lag_days": 0,
         "description": "Dovish = bonds rally, hawkish = bonds sell (direction flips with surprise sign)",
         "data_validated": False},
        {"asset": "UUP", "wave": 1, "direction": -1, "expected_car_5d": -0.005, "confidence": "low", "lag_days": 0,
         "description": "Dovish = dollar weakens",
         "data_validated": False},
        {"asset": "XLE", "wave": 1, "direction": -1, "expected_car_5d": -0.0074, "confidence": "medium", "lag_days": 0,
         "description": "Energy sells off on FOMC day — data-confirmed",
         "data_validated": True, "n": 37, "p": 0.049},
        # Wave 2: Equities and sectors reposition
        {"asset": "SPY", "wave": 2, "direction": 1, "expected_car_5d": 0.01, "confidence": "low", "lag_days": 1,
         "description": "Dovish = equities rally on easier financial conditions",
         "data_validated": False},
        {"asset": "QQQ", "wave": 2, "direction": 1, "expected_car_5d": 0.015, "confidence": "low", "lag_days": 1,
         "description": "Growth stocks more rate-sensitive",
         "data_validated": False},
        {"asset": "XLF", "wave": 2, "direction": -1, "expected_car_5d": -0.013, "confidence": "high", "lag_days": 1,
         "description": "Financials lose -1.3% over 20d post-FOMC (largest sample N=37) — NIM compression or uncertainty",
         "data_validated": True, "n": 37, "p": 0.022},
        {"asset": "XLU", "wave": 2, "direction": 1, "expected_car_5d": 0.012, "confidence": "low", "lag_days": 1,
         "description": "Yield proxies rally on lower rates",
         "data_validated": False},
        # Wave 3: EM and credit follow
        {"asset": "EEM", "wave": 3, "direction": 1, "expected_car_5d": 0.012, "confidence": "low", "lag_days": 3,
         "description": "Weaker dollar helps EM",
         "data_validated": False},
        {"asset": "GLD", "wave": 3, "direction": 1, "expected_car_5d": 0.008, "confidence": "low", "lag_days": 3,
         "description": "Lower real yields support gold",
         "data_validated": False},
        {"asset": "HYG", "wave": 3, "direction": 1, "expected_car_5d": 0.005, "confidence": "low", "lag_days": 3,
         "description": "Easier conditions tighten credit spreads",
         "data_validated": False},
    ],

    # ──────────────────────────────────────────────────────────────────────
    # TARIFF_ANNOUNCE — New tariffs / trade escalation
    # Data: XLU +2.48% over 20d (strongest, p<0.001). GLD +2.65% over 20d.
    #       HYG UP +0.44% (counter-intuitive flight to yield). XLY -0.58%.
    # ──────────────────────────────────────────────────────────────────────
    "TARIFF_ANNOUNCE": [
        {"asset": "FXI", "wave": 1, "direction": -1, "expected_car_5d": -0.05, "confidence": "medium", "lag_days": 0,
         "description": "China equities hit hardest",
         "data_validated": False},
        {"asset": "EEM", "wave": 1, "direction": -1, "expected_car_5d": -0.025, "confidence": "low", "lag_days": 0,
         "description": "Broad EM selloff on trade fear",
         "data_validated": False},
        {"asset": "XLY", "wave": 1, "direction": -1, "expected_car_5d": -0.0058, "confidence": "medium", "lag_days": 1,
         "description": "Consumer discretionary hurt -0.58% over 5d by tariff cost pass-through",
         "data_validated": True, "n": 16, "p": 0.030},
        # Wave 2: Defensive rotation — XLU and GLD are the big winners
        {"asset": "XLU", "wave": 2, "direction": 1, "expected_car_5d": 0.0248, "confidence": "high", "lag_days": 1,
         "description": "Utilities +2.48% over 20d — strongest tariff signal (defensive rotation)",
         "data_validated": True, "n": 16, "p": 0.000},
        {"asset": "GLD", "wave": 2, "direction": 1, "expected_car_5d": 0.0265, "confidence": "high", "lag_days": 1,
         "description": "Gold +2.65% over 20d — confirmed safe haven during tariffs",
         "data_validated": True, "n": 15, "p": 0.006},
        {"asset": "TLT", "wave": 2, "direction": 1, "expected_car_5d": 0.01, "confidence": "low", "lag_days": 1,
         "description": "Flight to safety (theoretical, not significant in data)",
         "data_validated": False},
        {"asset": "HYG", "wave": 2, "direction": 1, "expected_car_5d": 0.0044, "confidence": "medium", "lag_days": 1,
         "description": "HYG UP +0.44% over 20d — counter-intuitive flight to yield during tariffs",
         "data_validated": True, "n": 15, "p": 0.012},
        # Wave 3: Broader effects
        {"asset": "SPY", "wave": 3, "direction": -1, "expected_car_5d": -0.02, "confidence": "low", "lag_days": 3,
         "description": "Broad equity risk-off if tariffs are significant",
         "data_validated": False},
        {"asset": "QQQ", "wave": 3, "direction": -1, "expected_car_5d": -0.025, "confidence": "low", "lag_days": 3,
         "description": "Tech/semis supply chain disruption",
         "data_validated": False},
        {"asset": "XLI", "wave": 3, "direction": -1, "expected_car_5d": -0.03, "confidence": "low", "lag_days": 3,
         "description": "Industrials supply chain disruption (not significant in data)",
         "data_validated": False},
    ],

    # ──────────────────────────────────────────────────────────────────────
    # TRADE_DEESCALATION — Tariff rollback / trade deal progress
    # Data: XLU +1.37% (5d). XLK +1.40% (20d). LQD +1.23% (20d).
    #       XLE -4.54% (20d) and USO -7.91% (20d) — energy risk premium unwinds.
    # ──────────────────────────────────────────────────────────────────────
    "TRADE_DEESCALATION": [
        {"asset": "FXI", "wave": 1, "direction": 1, "expected_car_5d": 0.04, "confidence": "low", "lag_days": 0,
         "description": "China equities rally on reduced trade friction (theoretical)",
         "data_validated": False},
        {"asset": "EEM", "wave": 1, "direction": 1, "expected_car_5d": 0.02, "confidence": "low", "lag_days": 0,
         "description": "Broad EM rally on trade optimism (theoretical)",
         "data_validated": False},
        # Wave 2: Confirmed winners — defensives and quality
        {"asset": "XLU", "wave": 2, "direction": 1, "expected_car_5d": 0.0137, "confidence": "high", "lag_days": 1,
         "description": "Utilities +1.37% over 5d on de-escalation",
         "data_validated": True, "n": 6, "p": 0.016},
        {"asset": "XLK", "wave": 2, "direction": 1, "expected_car_5d": 0.014, "confidence": "medium", "lag_days": 1,
         "description": "Tech +1.40% over 20d — supply chain relief",
         "data_validated": True, "n": 6, "p": 0.031},
        {"asset": "LQD", "wave": 2, "direction": 1, "expected_car_5d": 0.0123, "confidence": "medium", "lag_days": 1,
         "description": "IG credit +1.23% over 20d — risk normalization",
         "data_validated": True, "n": 6, "p": 0.038},
        {"asset": "TLT", "wave": 2, "direction": -1, "expected_car_5d": -0.008, "confidence": "low", "lag_days": 1,
         "description": "Safety bid unwinds (theoretical)",
         "data_validated": False},
        {"asset": "GLD", "wave": 2, "direction": -1, "expected_car_5d": -0.008, "confidence": "low", "lag_days": 1,
         "description": "Safe haven unwind (theoretical)",
         "data_validated": False},
        # Wave 3: Energy SELLS OFF — risk premium unwind is the big surprise
        {"asset": "XLE", "wave": 3, "direction": -1, "expected_car_5d": -0.0454, "confidence": "medium", "lag_days": 3,
         "description": "Energy -4.54% over 20d — geopolitical risk premium unwinds on de-escalation",
         "data_validated": True, "n": 6, "p": 0.041},
        {"asset": "USO", "wave": 3, "direction": -1, "expected_car_5d": -0.0791, "confidence": "medium", "lag_days": 3,
         "description": "Oil -7.91% over 20d — supply disruption fear evaporates",
         "data_validated": True, "n": 6, "p": 0.046},
        {"asset": "SPY", "wave": 3, "direction": 1, "expected_car_5d": 0.015, "confidence": "low", "lag_days": 3,
         "description": "Broad equity risk-on (theoretical)",
         "data_validated": False},
    ],

    # ──────────────────────────────────────────────────────────────────────
    # OPEC_CUT — Production cut announcement
    # Data: FXI -1.95% over 5d (p=0.011) — China as oil importer is hit.
    # ──────────────────────────────────────────────────────────────────────
    "OPEC_CUT": [
        {"asset": "USO", "wave": 1, "direction": 1, "expected_car_5d": 0.05, "confidence": "medium", "lag_days": 0,
         "description": "Oil directly up on supply cut",
         "data_validated": False},
        {"asset": "XLE", "wave": 1, "direction": 1, "expected_car_5d": 0.035, "confidence": "medium", "lag_days": 0,
         "description": "Energy stocks follow oil",
         "data_validated": False},
        {"asset": "FXI", "wave": 2, "direction": -1, "expected_car_5d": -0.0195, "confidence": "high", "lag_days": 2,
         "description": "China equities -1.95% over 5d — major oil importer hurt by higher prices",
         "data_validated": True, "n": 11, "p": 0.011},
        {"asset": "XLI", "wave": 2, "direction": -1, "expected_car_5d": -0.01, "confidence": "low", "lag_days": 2,
         "description": "Higher input costs for industrials (theoretical)",
         "data_validated": False},
        {"asset": "XLY", "wave": 2, "direction": -1, "expected_car_5d": -0.01, "confidence": "low", "lag_days": 2,
         "description": "Consumer discretionary hurt by energy costs (theoretical)",
         "data_validated": False},
        {"asset": "TLT", "wave": 3, "direction": -1, "expected_car_5d": -0.008, "confidence": "low", "lag_days": 5,
         "description": "Inflation expectations rise, bonds sell (theoretical)",
         "data_validated": False},
        {"asset": "EEM", "wave": 3, "direction": -1, "expected_car_5d": -0.015, "confidence": "low", "lag_days": 5,
         "description": "Oil importers in EM hurt (theoretical)",
         "data_validated": False},
    ],

    # ──────────────────────────────────────────────────────────────────────
    # OPEC_INCREASE — Production increase / quota raise
    # No statistically significant data yet — all theoretical.
    # ──────────────────────────────────────────────────────────────────────
    "OPEC_INCREASE": [
        {"asset": "USO", "wave": 1, "direction": -1, "expected_car_5d": -0.04, "confidence": "medium", "lag_days": 0,
         "description": "Oil directly down on supply increase",
         "data_validated": False},
        {"asset": "XLE", "wave": 1, "direction": -1, "expected_car_5d": -0.03, "confidence": "medium", "lag_days": 0,
         "description": "Energy stocks follow oil lower",
         "data_validated": False},
        {"asset": "XLI", "wave": 2, "direction": 1, "expected_car_5d": 0.008, "confidence": "low", "lag_days": 2,
         "description": "Lower input costs benefit industrials",
         "data_validated": False},
        {"asset": "XLY", "wave": 2, "direction": 1, "expected_car_5d": 0.008, "confidence": "low", "lag_days": 2,
         "description": "Consumer discretionary benefits from lower energy costs",
         "data_validated": False},
        {"asset": "TLT", "wave": 3, "direction": 1, "expected_car_5d": 0.006, "confidence": "low", "lag_days": 5,
         "description": "Inflation expectations ease, bonds rally",
         "data_validated": False},
        {"asset": "EEM", "wave": 3, "direction": 1, "expected_car_5d": 0.012, "confidence": "low", "lag_days": 5,
         "description": "Oil importers in EM benefit",
         "data_validated": False},
    ],

    # ──────────────────────────────────────────────────────────────────────
    # OIL_SUPPLY_DISRUPTION — Pipeline attack, strait closure, etc.
    # No statistically significant data yet — all theoretical.
    # ──────────────────────────────────────────────────────────────────────
    "OIL_SUPPLY_DISRUPTION": [
        {"asset": "USO", "wave": 1, "direction": 1, "expected_car_5d": 0.08, "confidence": "medium", "lag_days": 0,
         "description": "Oil spikes on acute supply loss",
         "data_validated": False},
        {"asset": "XLE", "wave": 1, "direction": 1, "expected_car_5d": 0.05, "confidence": "medium", "lag_days": 0,
         "description": "Energy stocks surge",
         "data_validated": False},
        {"asset": "GLD", "wave": 1, "direction": 1, "expected_car_5d": 0.02, "confidence": "low", "lag_days": 0,
         "description": "Geopolitical safe haven bid",
         "data_validated": False},
        {"asset": "SPY", "wave": 2, "direction": -1, "expected_car_5d": -0.02, "confidence": "low", "lag_days": 1,
         "description": "Broad risk-off on supply shock fear",
         "data_validated": False},
        {"asset": "XLI", "wave": 2, "direction": -1, "expected_car_5d": -0.02, "confidence": "low", "lag_days": 2,
         "description": "Industrials hammered by input cost spike",
         "data_validated": False},
        {"asset": "XLY", "wave": 2, "direction": -1, "expected_car_5d": -0.015, "confidence": "low", "lag_days": 2,
         "description": "Consumer spending hit by fuel costs",
         "data_validated": False},
        {"asset": "TLT", "wave": 3, "direction": -1, "expected_car_5d": -0.012, "confidence": "low", "lag_days": 5,
         "description": "Inflation expectations spike, bonds sell",
         "data_validated": False},
        {"asset": "EEM", "wave": 3, "direction": -1, "expected_car_5d": -0.025, "confidence": "low", "lag_days": 5,
         "description": "EM oil importers severely impacted",
         "data_validated": False},
    ],

    # ──────────────────────────────────────────────────────────────────────
    # MILITARY_CONFLICT — Armed conflict / war escalation
    # Data CONTRADICTS template on TLT: TLT DOWN -0.49% on day 1 (not up).
    # EWJ is the real victim (-2.4% over 20d), not broad EEM.
    # FXY weakens (yen NOT a safe haven here).
    # ──────────────────────────────────────────────────────────────────────
    "MILITARY_CONFLICT": [
        {"asset": "GLD", "wave": 1, "direction": 1, "expected_car_5d": 0.03, "confidence": "low", "lag_days": 0,
         "description": "Gold safe haven bid (theoretical — not significant in data)",
         "data_validated": False},
        {"asset": "USO", "wave": 1, "direction": 1, "expected_car_5d": 0.05, "confidence": "low", "lag_days": 0,
         "description": "Oil supply disruption fear (theoretical)",
         "data_validated": False},
        {"asset": "SPY", "wave": 1, "direction": -1, "expected_car_5d": -0.03, "confidence": "low", "lag_days": 0,
         "description": "Risk-off (theoretical — not significant in data)",
         "data_validated": False},
        {"asset": "TLT", "wave": 1, "direction": -1, "expected_car_5d": -0.0049, "confidence": "medium", "lag_days": 0,
         "description": "TLT DOWN -0.49% on day 1 — DATA CONTRADICTS flight-to-safety assumption",
         "data_validated": True, "n": 14, "p": 0.037},
        # Wave 2: Japan and yen are the real victims
        {"asset": "FXY", "wave": 2, "direction": -1, "expected_car_5d": -0.0054, "confidence": "high", "lag_days": 1,
         "description": "Yen weakens -0.54% over 5d — NOT a safe haven during conflict",
         "data_validated": True, "n": 12, "p": 0.001},
        {"asset": "EWJ", "wave": 2, "direction": -1, "expected_car_5d": -0.0242, "confidence": "high", "lag_days": 1,
         "description": "Japan equities -2.42% over 20d — primary conflict casualty, not broad EM",
         "data_validated": True, "n": 14, "p": 0.003},
        {"asset": "XLE", "wave": 2, "direction": 1, "expected_car_5d": 0.04, "confidence": "low", "lag_days": 1,
         "description": "Energy sector benefits from oil spike (theoretical)",
         "data_validated": False},
        # Wave 3: removed IWM (not significant), EEM demoted
        {"asset": "EEM", "wave": 3, "direction": -1, "expected_car_5d": -0.03, "confidence": "low", "lag_days": 3,
         "description": "EM risk-off (theoretical — EWJ is the real victim per data)",
         "data_validated": False},
    ],

    # ──────────────────────────────────────────────────────────────────────
    # CPI_RELEASE — Hot CPI baseline (flip for cool)
    # Data SURPRISES: XLK UP +2.08% over 20d. QQQ UP +0.92% day 1.
    # USO UP +0.90% day 1. Counter-intuitive: tech/growth rally after CPI.
    # ──────────────────────────────────────────────────────────────────────
    "CPI_RELEASE": [
        {"asset": "TLT", "wave": 1, "direction": -1, "expected_car_5d": -0.01, "confidence": "low", "lag_days": 0,
         "description": "Hot CPI = rates stay high, bonds sell (theoretical — not significant in data)",
         "data_validated": False},
        {"asset": "QQQ", "wave": 1, "direction": 1, "expected_car_5d": 0.0092, "confidence": "medium", "lag_days": 0,
         "description": "QQQ UP +0.92% on CPI day — DATA CONTRADICTS rate-fear assumption",
         "data_validated": True, "n": 8, "p": 0.033},
        {"asset": "USO", "wave": 1, "direction": 1, "expected_car_5d": 0.009, "confidence": "medium", "lag_days": 0,
         "description": "Oil +0.90% on CPI day — inflation confirms commodity strength",
         "data_validated": True, "n": 8, "p": 0.049},
        # Wave 2: Tech is the big winner over 20d
        {"asset": "XLK", "wave": 2, "direction": 1, "expected_car_5d": 0.0208, "confidence": "high", "lag_days": 1,
         "description": "Tech +2.08% over 20d post-CPI — counter-intuitive growth outperformance",
         "data_validated": True, "n": 8, "p": 0.004},
        {"asset": "GLD", "wave": 2, "direction": -1, "expected_car_5d": -0.008, "confidence": "low", "lag_days": 1,
         "description": "Higher real yields hurt gold (theoretical)",
         "data_validated": False},
        {"asset": "XLU", "wave": 2, "direction": -1, "expected_car_5d": -0.01, "confidence": "low", "lag_days": 1,
         "description": "Yield proxies hurt by higher rates (theoretical — not significant)",
         "data_validated": False},
    ],

    # ──────────────────────────────────────────────────────────────────────
    # NFP_RELEASE — Strong NFP baseline (flip for weak)
    # Data: EWJ +0.86% day 1. EEM +2.62% over 20d (OPPOSITE of template).
    # XLU +0.71% over 5d.
    # ──────────────────────────────────────────────────────────────────────
    "NFP_RELEASE": [
        # Wave 1: Immediate reactions
        {"asset": "TLT", "wave": 1, "direction": -1, "expected_car_5d": -0.012, "confidence": "low", "lag_days": 0,
         "description": "Strong jobs = Fed stays hawkish, bonds sell (theoretical)",
         "data_validated": False},
        {"asset": "UUP", "wave": 1, "direction": 1, "expected_car_5d": 0.004, "confidence": "low", "lag_days": 0,
         "description": "Strong jobs = dollar strengthens (theoretical)",
         "data_validated": False},
        {"asset": "EWJ", "wave": 1, "direction": 1, "expected_car_5d": 0.0086, "confidence": "high", "lag_days": 0,
         "description": "Japan equities +0.86% on NFP day — strong US economy lifts Japan export outlook",
         "data_validated": True, "n": 7, "p": 0.002},
        # Wave 2: Sector and defensive rotation
        {"asset": "XLU", "wave": 2, "direction": 1, "expected_car_5d": 0.0071, "confidence": "medium", "lag_days": 1,
         "description": "Utilities +0.71% over 5d post-NFP",
         "data_validated": True, "n": 7, "p": 0.027},
        {"asset": "SPY", "wave": 2, "direction": 1, "expected_car_5d": 0.008, "confidence": "low", "lag_days": 1,
         "description": "Strong economy supports equities (theoretical)",
         "data_validated": False},
        {"asset": "XLF", "wave": 2, "direction": 1, "expected_car_5d": 0.01, "confidence": "low", "lag_days": 1,
         "description": "Banks benefit from higher-for-longer rates (theoretical)",
         "data_validated": False},
        # Wave 3: EM is UP, not down — data contradicts template
        {"asset": "EEM", "wave": 3, "direction": 1, "expected_car_5d": 0.0262, "confidence": "medium", "lag_days": 3,
         "description": "EM UP +2.62% over 20d — DATA CONTRADICTS strong-dollar-hurts-EM assumption",
         "data_validated": True, "n": 7, "p": 0.024},
        {"asset": "GLD", "wave": 3, "direction": -1, "expected_car_5d": -0.006, "confidence": "low", "lag_days": 3,
         "description": "Higher real yields pressure gold (theoretical)",
         "data_validated": False},
    ],

    # ──────────────────────────────────────────────────────────────────────
    # BANK_FAILURE — Single institution failure / contagion risk
    # Data: XLI is the biggest victim (-3.08% over 20d, not XLF).
    # XLK goes UP +0.50% (tech = safe haven in bank stress).
    # USO drops -2.65% day 1. XLV -0.69% over 5d. FXY -1.55% over 20d.
    # ──────────────────────────────────────────────────────────────────────
    "BANK_FAILURE": [
        # Wave 1: Immediate shocks
        {"asset": "XLK", "wave": 1, "direction": 1, "expected_car_5d": 0.005, "confidence": "high", "lag_days": 0,
         "description": "Tech UP +0.50% day 1 — acts as safe haven during bank stress",
         "data_validated": True, "n": 5, "p": 0.002},
        {"asset": "USO", "wave": 1, "direction": -1, "expected_car_5d": -0.0265, "confidence": "high", "lag_days": 0,
         "description": "Oil DOWN -2.65% day 1 — demand destruction fear",
         "data_validated": True, "n": 5, "p": 0.022},
        {"asset": "XLF", "wave": 1, "direction": -1, "expected_car_5d": -0.08, "confidence": "medium", "lag_days": 0,
         "description": "Bank sector contagion fear (theoretical — not top signal in data)",
         "data_validated": False},
        {"asset": "SPY", "wave": 1, "direction": -1, "expected_car_5d": -0.03, "confidence": "low", "lag_days": 0,
         "description": "Broad risk-off (theoretical)",
         "data_validated": False},
        {"asset": "TLT", "wave": 1, "direction": 1, "expected_car_5d": 0.03, "confidence": "low", "lag_days": 0,
         "description": "Flight to safety + rate cut expectations (theoretical)",
         "data_validated": False},
        # Wave 2: Secondary victims
        {"asset": "XLV", "wave": 2, "direction": -1, "expected_car_5d": -0.0069, "confidence": "medium", "lag_days": 1,
         "description": "Healthcare -0.69% over 5d — delayed contagion to defensive sectors",
         "data_validated": True, "n": 5, "p": 0.043},
        {"asset": "XLI", "wave": 2, "direction": -1, "expected_car_5d": -0.0308, "confidence": "high", "lag_days": 1,
         "description": "Industrials -3.08% over 20d — BIGGER victim than XLF in bank failures",
         "data_validated": True, "n": 5, "p": 0.006},
        {"asset": "FXY", "wave": 2, "direction": -1, "expected_car_5d": -0.0155, "confidence": "medium", "lag_days": 1,
         "description": "Yen weakens -1.55% over 20d — risk-off doesn't help yen during bank stress",
         "data_validated": True, "n": 5, "p": 0.033},
        # Wave 3: removed HYG (not significant), IWM (not significant), GLD (not significant)
        {"asset": "HYG", "wave": 3, "direction": -1, "expected_car_5d": -0.02, "confidence": "low", "lag_days": 3,
         "description": "Credit spreads widen (theoretical — not significant in data)",
         "data_validated": False},
        {"asset": "GLD", "wave": 3, "direction": 1, "expected_car_5d": 0.02, "confidence": "low", "lag_days": 3,
         "description": "Rate cut expectations + safety (theoretical)",
         "data_validated": False},
    ],

    # ──────────────────────────────────────────────────────────────────────
    # CREDIT_CRISIS — Systemic credit event (worse than single bank failure)
    # No statistically significant data yet — all theoretical.
    # ──────────────────────────────────────────────────────────────────────
    "CREDIT_CRISIS": [
        {"asset": "XLF", "wave": 1, "direction": -1, "expected_car_5d": -0.12, "confidence": "medium", "lag_days": 0,
         "description": "Financial sector in crisis mode",
         "data_validated": False},
        {"asset": "SPY", "wave": 1, "direction": -1, "expected_car_5d": -0.06, "confidence": "medium", "lag_days": 0,
         "description": "Severe broad market risk-off",
         "data_validated": False},
        {"asset": "HYG", "wave": 1, "direction": -1, "expected_car_5d": -0.05, "confidence": "medium", "lag_days": 0,
         "description": "Credit spreads blow out immediately",
         "data_validated": False},
        {"asset": "TLT", "wave": 1, "direction": 1, "expected_car_5d": 0.04, "confidence": "medium", "lag_days": 0,
         "description": "Massive flight to safety",
         "data_validated": False},
        {"asset": "IWM", "wave": 2, "direction": -1, "expected_car_5d": -0.08, "confidence": "medium", "lag_days": 1,
         "description": "Small caps devastated by credit freeze",
         "data_validated": False},
        {"asset": "EEM", "wave": 2, "direction": -1, "expected_car_5d": -0.06, "confidence": "medium", "lag_days": 1,
         "description": "EM capital flight accelerates",
         "data_validated": False},
        {"asset": "GLD", "wave": 2, "direction": 1, "expected_car_5d": 0.03, "confidence": "low", "lag_days": 1,
         "description": "Gold safe haven bid intensifies",
         "data_validated": False},
        {"asset": "XLI", "wave": 3, "direction": -1, "expected_car_5d": -0.05, "confidence": "low", "lag_days": 3,
         "description": "Industrial activity collapses on frozen credit",
         "data_validated": False},
        {"asset": "XLY", "wave": 3, "direction": -1, "expected_car_5d": -0.04, "confidence": "low", "lag_days": 3,
         "description": "Consumer spending cratered",
         "data_validated": False},
    ],

    # ──────────────────────────────────────────────────────────────────────
    # PANDEMIC — Global health crisis
    # No statistically significant data yet — all theoretical.
    # ──────────────────────────────────────────────────────────────────────
    "PANDEMIC": [
        {"asset": "SPY", "wave": 1, "direction": -1, "expected_car_5d": -0.10, "confidence": "medium", "lag_days": 0,
         "description": "Extreme risk-off",
         "data_validated": False},
        {"asset": "GLD", "wave": 1, "direction": 1, "expected_car_5d": 0.03, "confidence": "low", "lag_days": 0,
         "description": "Safe haven",
         "data_validated": False},
        {"asset": "TLT", "wave": 1, "direction": 1, "expected_car_5d": 0.05, "confidence": "low", "lag_days": 0,
         "description": "Flight to safety",
         "data_validated": False},
        {"asset": "HYG", "wave": 2, "direction": -1, "expected_car_5d": -0.08, "confidence": "medium", "lag_days": 1,
         "description": "Credit market stress",
         "data_validated": False},
        {"asset": "EEM", "wave": 2, "direction": -1, "expected_car_5d": -0.08, "confidence": "medium", "lag_days": 1,
         "description": "EM capital flight",
         "data_validated": False},
        {"asset": "XLE", "wave": 3, "direction": -1, "expected_car_5d": -0.10, "confidence": "low", "lag_days": 3,
         "description": "Demand destruction",
         "data_validated": False},
    ],

    # ──────────────────────────────────────────────────────────────────────
    # SANCTIONS — Economic sanctions on a country
    # No statistically significant data yet — all theoretical.
    # ──────────────────────────────────────────────────────────────────────
    "SANCTIONS": [
        {"asset": "USO", "wave": 1, "direction": 1, "expected_car_5d": 0.04, "confidence": "low", "lag_days": 0,
         "description": "Supply disruption if energy-producing country",
         "data_validated": False},
        {"asset": "GLD", "wave": 1, "direction": 1, "expected_car_5d": 0.02, "confidence": "low", "lag_days": 0,
         "description": "Geopolitical uncertainty",
         "data_validated": False},
        {"asset": "EEM", "wave": 2, "direction": -1, "expected_car_5d": -0.02, "confidence": "low", "lag_days": 1,
         "description": "EM risk premium rises",
         "data_validated": False},
    ],

    # ──────────────────────────────────────────────────────────────────────
    # ELECTION_RESULT — Major election outcome (pro-business baseline)
    # No statistically significant data yet — all theoretical.
    # ──────────────────────────────────────────────────────────────────────
    "ELECTION_RESULT": [
        # Wave 1: Equity and FX react to policy expectations
        {"asset": "SPY", "wave": 1, "direction": 1, "expected_car_5d": 0.015, "confidence": "low", "lag_days": 0,
         "description": "Pro-business outcome = equity rally on deregulation/tax expectations",
         "data_validated": False},
        {"asset": "UUP", "wave": 1, "direction": 1, "expected_car_5d": 0.005, "confidence": "low", "lag_days": 0,
         "description": "Dollar strengthens on growth expectations",
         "data_validated": False},
        {"asset": "IWM", "wave": 1, "direction": 1, "expected_car_5d": 0.02, "confidence": "low", "lag_days": 0,
         "description": "Small caps benefit from domestic policy focus",
         "data_validated": False},
        # Wave 2: Sector rotation based on policy
        {"asset": "XLF", "wave": 2, "direction": 1, "expected_car_5d": 0.02, "confidence": "low", "lag_days": 1,
         "description": "Financials rally on deregulation expectations",
         "data_validated": False},
        {"asset": "XLE", "wave": 2, "direction": 1, "expected_car_5d": 0.015, "confidence": "low", "lag_days": 1,
         "description": "Energy benefits from reduced regulation",
         "data_validated": False},
        {"asset": "XLU", "wave": 2, "direction": -1, "expected_car_5d": -0.008, "confidence": "low", "lag_days": 1,
         "description": "Defensives underperform in risk-on rotation",
         "data_validated": False},
        # Wave 3: Rates adjust to fiscal expectations
        {"asset": "TLT", "wave": 3, "direction": -1, "expected_car_5d": -0.01, "confidence": "low", "lag_days": 3,
         "description": "Bonds sell on deficit spending / growth expectations",
         "data_validated": False},
        {"asset": "EEM", "wave": 3, "direction": -1, "expected_car_5d": -0.01, "confidence": "low", "lag_days": 3,
         "description": "Stronger dollar + trade policy uncertainty weighs on EM",
         "data_validated": False},
    ],
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_cascade_template(event_type: str) -> list[dict] | None:
    """Look up the cascade template for an event type.

    Returns the list of impact dicts, or None if no template exists.
    """
    return TEMPLATES.get(event_type)


def get_affected_assets(event_type: str) -> list[str]:
    """Return a deduplicated list of all assets affected by *event_type*."""
    template = TEMPLATES.get(event_type)
    if not template:
        return []
    seen: set[str] = set()
    result: list[str] = []
    for entry in template:
        if entry["asset"] not in seen:
            seen.add(entry["asset"])
            result.append(entry["asset"])
    return result


def get_wave_n_assets(event_type: str, wave: int) -> list[dict]:
    """Return the impact entries for a specific wave number."""
    template = TEMPLATES.get(event_type)
    if not template:
        return []
    return [e for e in template if e["wave"] == wave]


def get_validated_entries(event_type: str) -> list[dict]:
    """Return only data-validated entries for an event type."""
    template = TEMPLATES.get(event_type)
    if not template:
        return []
    return [e for e in template if e.get("data_validated", False)]


def format_cascade_forecast(event_type: str, severity: float = 1.0) -> str:
    """Build a human-readable cascade forecast string for Discord alerts.

    *severity* scales the expected_car values (e.g. 1.5 = 50% worse than
    baseline).  Returns a multi-line string organised by wave with timing
    info.
    """
    template = TEMPLATES.get(event_type)
    if not template:
        return f"No cascade template found for event type: {event_type}"

    lines: list[str] = [f"=== CASCADE FORECAST: {event_type} (severity {severity:.1f}x) ==="]

    # Group by wave
    waves: dict[int, list[dict]] = {}
    for entry in template:
        waves.setdefault(entry["wave"], []).append(entry)

    for wave_num in sorted(waves):
        entries = waves[wave_num]
        lag = entries[0]["lag_days"]
        timing = "IMMEDIATE" if lag == 0 else f"+{lag}d"
        lines.append(f"\n-- Wave {wave_num} ({timing}) --")
        for e in entries:
            scaled_car = e["expected_car_5d"] * severity
            direction_str = "UP" if e["direction"] > 0 else "DOWN"
            conf = e["confidence"].upper()
            validated = " [DATA]" if e.get("data_validated") else ""
            lines.append(
                f"  {e['asset']:5s} {direction_str:4s} | CAR(5d): {scaled_car:+.1%} | "
                f"conf: {conf:6s}{validated} | {e['description']}"
            )

    return "\n".join(lines)
