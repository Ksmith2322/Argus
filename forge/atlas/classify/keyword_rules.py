"""
Keyword-based event classifier for Atlas headlines.

Maps headlines to event types and categories using rule-based matching.
Rules are evaluated top-to-bottom; first match wins.
"""

import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rule table
# ---------------------------------------------------------------------------
# Each rule: (keywords_any, keywords_all, event_type, event_category, base_severity)
#   keywords_any  -- headline must contain ANY of these (case-insensitive)
#   keywords_all  -- headline must contain ALL of these (empty = no requirement)
#   First match wins (evaluated top-to-bottom)

RULES: list[tuple[list[str], list[str], str, str, float]] = [
    # ---- Monetary Policy ----
    (["federal reserve", "fomc", "fed rate", "interest rate", "rate decision",
      "rate cut", "rate hike", "powell"],
     [], "FOMC_DECISION", "MONETARY", 0.6),
    (["ecb", "european central bank", "lagarde"],
     [], "ECB_DECISION", "MONETARY", 0.5),
    (["bank of japan", "boj", "yield curve control", "yen intervention"],
     [], "BOJ_DECISION", "MONETARY", 0.6),
    (["pboc", "people's bank", "china rate", "rrr cut"],
     [], "PBOC_DECISION", "MONETARY", 0.5),
    (["quantitative easing", "quantitative tightening", "balance sheet", "qe", "qt"],
     [], "QE_QT_CHANGE", "MONETARY", 0.5),

    # ---- Oil / Energy ----
    (["opec", "oil production", "barrel", "crude oil"],
     ["cut", "reduce", "slash"], "OPEC_CUT", "ENERGY", 0.7),
    (["opec", "oil production"],
     ["increase", "boost", "raise"], "OPEC_INCREASE", "ENERGY", 0.6),
    (["oil price", "crude oil", "oil spike", "oil crash", "oil surge", "brent"],
     [], "OIL_MOVE", "ENERGY", 0.5),
    (["strait of hormuz", "pipeline attack", "refinery attack", "oil supply disruption"],
     [], "OIL_SUPPLY_DISRUPTION", "ENERGY", 0.8),
    (["natural gas", "lng", "gas price"],
     [], "NATGAS_MOVE", "ENERGY", 0.4),

    # ---- Trade / Tariffs ----
    (["tariff", "trade war", "import duty", "customs duty", "trade barrier"],
     [], "TARIFF_ANNOUNCE", "TRADE", 0.7),
    (["trade deal", "trade agreement", "trade truce", "tariff removal", "tariff reduction"],
     [], "TRADE_DEESCALATION", "TRADE", 0.6),
    (["sanctions imposed", "new sanctions", "sanctions against", "sanctioned by",
      "embargo on", "asset freeze", "sanctions package", "sanctions regime"],
     [], "SANCTIONS", "TRADE", 0.7),
    (["export control", "chip ban", "technology ban", "export restriction"],
     [], "EXPORT_CONTROLS", "TRADE", 0.6),

    # ---- Geopolitical ----
    (["invasion", "troops entered", "military offensive", "declared war",
      "missile strike", "bombing"],
     [], "MILITARY_CONFLICT", "GEOPOLITICAL", 0.9),
    (["ceasefire", "peace deal", "peace talks", "de-escalation", "troops withdraw"],
     [], "DEESCALATION", "GEOPOLITICAL", 0.6),
    (["terror attack", "terrorist", "bombing attack"],
     [], "TERROR_ATTACK", "GEOPOLITICAL", 0.8),
    (["taiwan", "china military", "south china sea"],
     [], "TAIWAN_TENSION", "GEOPOLITICAL", 0.7),
    (["nuclear weapon", "nuclear threat", "nuclear test", "missile test", "north korea test",
      "nuclear stand-off", "nuclear standoff", "nuclear warhead"],
     [], "NUCLEAR_THREAT", "GEOPOLITICAL", 0.8),

    # ---- Economic Data ----
    (["cpi", "consumer price", "inflation rate", "inflation data"],
     [], "CPI_RELEASE", "ECONOMIC_DATA", 0.5),
    (["jobs report", "nonfarm payroll", "nfp", "employment data", "unemployment rate"],
     [], "NFP_RELEASE", "ECONOMIC_DATA", 0.5),
    (["gdp", "gross domestic product", "economic growth"],
     [], "GDP_RELEASE", "ECONOMIC_DATA", 0.4),
    (["pmi", "manufacturing index", "ism manufacturing", "services index"],
     [], "PMI_RELEASE", "ECONOMIC_DATA", 0.3),

    # ---- Financial / Banking ----
    (["bank failure", "bank collapse", "bank run", "fdic", "bank seized"],
     [], "BANK_FAILURE", "FINANCIAL", 0.9),
    (["credit crisis", "credit crunch", "liquidity crisis", "money market"],
     [], "CREDIT_CRISIS", "FINANCIAL", 0.9),
    (["downgrade", "credit rating", "moody's", "s&p downgrade", "fitch"],
     [], "CREDIT_DOWNGRADE", "FINANCIAL", 0.5),

    # ---- Elections / Political ----
    (["election result", "elected president", "election surprise", "election outcome"],
     [], "ELECTION_RESULT", "POLITICAL", 0.5),
    (["government shutdown", "debt ceiling", "default risk"],
     [], "FISCAL_CRISIS", "POLITICAL", 0.6),

    # ---- Pandemic / Black Swan ----
    (["pandemic", "outbreak", "epidemic", "virus spread", "who emergency"],
     [], "PANDEMIC", "BLACK_SWAN", 0.9),
    (["earthquake", "tsunami", "hurricane", "typhoon", "wildfire"],
     [], "NATURAL_DISASTER", "BLACK_SWAN", 0.5),
]

# ---------------------------------------------------------------------------
# Severity adjusters
# ---------------------------------------------------------------------------

_SEVERITY_BOOST: dict[str, float] = {
    "crash": 0.15,
    "emergency": 0.15,
    "unprecedented": 0.1,
    "war": 0.1,
    "crisis": 0.1,
}

_SEVERITY_REDUCE: list[str] = ["could", "may", "might"]
_SEVERITY_REDUCE_AMOUNT: float = 0.1


def adjust_severity(base_severity: float, title: str) -> float:
    """Adjust base severity based on extreme/hedging keywords.

    Boosts for: crash (+0.15), emergency (+0.15), unprecedented (+0.1),
                war (+0.1), crisis (+0.1)
    Reduces for: could, may, might (-0.1 each)
    Clamped to [0.0, 1.0].
    """
    title_lower = title.lower()
    severity = base_severity

    for word, boost in _SEVERITY_BOOST.items():
        if word in title_lower:
            severity += boost

    for word in _SEVERITY_REDUCE:
        if word in title_lower:
            severity -= _SEVERITY_REDUCE_AMOUNT

    return max(0.0, min(1.0, severity))


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _text_contains_any(text: str, keywords: list[str]) -> bool:
    """Return True if text contains ANY of the keywords (case-insensitive)."""
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in keywords)


def _text_contains_all(text: str, keywords: list[str]) -> bool:
    """Return True if text contains ALL of the keywords (case-insensitive)."""
    text_lower = text.lower()
    return all(kw.lower() in text_lower for kw in keywords)


def classify_headline(title: str, summary: str = "") -> dict | None:
    """Classify a headline into an event type and category.

    Checks both title and summary for keyword matches.
    Returns dict with {event_type, event_category, severity} or None.
    """
    combined = f"{title} {summary}".strip()
    if not combined:
        return None

    for keywords_any, keywords_all, event_type, event_category, base_severity in RULES:
        # Must match ANY of keywords_any
        if not _text_contains_any(combined, keywords_any):
            continue

        # Must match ALL of keywords_all (if non-empty)
        if keywords_all and not _text_contains_all(combined, keywords_all):
            continue

        # Match found
        severity = adjust_severity(base_severity, title)
        return {
            "event_type": event_type,
            "event_category": event_category,
            "severity": round(severity, 2),
        }

    return None
