"""
Severity enhancement for Atlas event classification.

Provides additional severity scoring beyond the base keyword rules,
using amplifiers, dampeners, and superlative detection.
"""

import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Severity modifiers
# ---------------------------------------------------------------------------

# Extreme amplifiers: +0.1 to +0.2
_EXTREME_AMPLIFIERS: dict[str, float] = {
    "crash": 0.2,
    "collapse": 0.2,
    "war": 0.15,
    "emergency": 0.15,
    "unprecedented": 0.15,
    "circuit breaker": 0.2,
    "black swan": 0.2,
}

# Moderate amplifiers: +0.05 to +0.1
_MODERATE_AMPLIFIERS: dict[str, float] = {
    "surge": 0.1,
    "plunge": 0.1,
    "spike": 0.08,
    "crisis": 0.1,
    "shock": 0.08,
    "plummet": 0.1,
    "soar": 0.05,
    "tumble": 0.08,
    "panic": 0.1,
    "meltdown": 0.1,
}

# Dampeners: -0.1 each
_DAMPENERS: list[str] = [
    "could",
    "may",
    "might",
    "considers",
    "discusses",
    "rumor",
    "rumour",
    "speculation",
    "reportedly",
    "allegedly",
]

# Superlatives: +0.1 each
_SUPERLATIVES: list[str] = [
    "worst since",
    "biggest",
    "largest",
    "record",
    "historic",
    "all-time",
    "first time",
    "never before",
    "most since",
]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_severity(title: str, summary: str = "", base: float = 0.5) -> float:
    """Compute enhanced severity score from headline text.

    Adjusts the base severity using extreme/moderate amplifiers,
    dampeners, and superlative detection.

    Parameters
    ----------
    title : str
        Headline text.
    summary : str, optional
        Article summary for additional context.
    base : float
        Starting severity score (typically from keyword_rules).

    Returns
    -------
    float
        Severity score clamped to [0.0, 1.0].
    """
    combined = f"{title} {summary}".lower()
    severity = base

    # Extreme amplifiers
    for keyword, boost in _EXTREME_AMPLIFIERS.items():
        if keyword in combined:
            severity += boost

    # Moderate amplifiers
    for keyword, boost in _MODERATE_AMPLIFIERS.items():
        if keyword in combined:
            severity += boost

    # Dampeners
    for keyword in _DAMPENERS:
        if keyword in combined:
            severity -= 0.1

    # Superlatives
    for phrase in _SUPERLATIVES:
        if phrase in combined:
            severity += 0.1

    return round(max(0.0, min(1.0, severity)), 2)
