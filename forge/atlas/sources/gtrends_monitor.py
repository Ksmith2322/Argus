"""
Google Trends keyword spike detector for Atlas event detection.

Uses pytrends to monitor search interest in crisis-related keywords.
Flags spikes where current interest exceeds the 7-day average by a
configurable threshold (default 2x).

Usage:
    python -m forge.atlas.sources.gtrends_monitor --test

Requires:
    pip install pytrends
"""

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Keyword groups to monitor
# ---------------------------------------------------------------------------

KEYWORD_GROUPS = {
    "market_crisis": ["stock market crash", "market crash", "recession"],
    "fed_policy": ["federal reserve", "interest rate cut", "rate hike"],
    "oil_crisis": ["oil price", "gas prices", "OPEC"],
    "geopolitical": ["world war", "nuclear war", "iran war"],
    "banking": ["bank run", "bank failure", "FDIC"],
    "inflation": ["inflation", "CPI", "consumer prices"],
    "trade_war": ["tariff", "trade war", "sanctions"],
}

# pytrends allows max 5 keywords per request
PYTRENDS_BATCH_SIZE = 5
RATE_LIMIT_DELAY = 5.0  # seconds between pytrends requests
MAX_RETRIES = 3
RETRY_BACKOFF = 10.0  # seconds, multiplied by attempt number


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

def _build_pytrends():
    """Create a TrendReq instance. Separated for testability."""
    from pytrends.request import TrendReq
    return TrendReq(hl="en-US", tz=360)


def check_keyword_spikes(
    keywords: list[str],
    timeframe: str = "now 7-d",
    pytrends_instance=None,
) -> dict:
    """Check Google Trends interest for keywords over the given timeframe.

    Parameters
    ----------
    keywords : list[str]
        Up to 5 keywords to query (pytrends limit).
    timeframe : str
        pytrends timeframe string (default: last 7 days).
    pytrends_instance : optional
        Pre-built TrendReq instance; created if not provided.

    Returns
    -------
    dict
        {keyword: {"current": int, "avg_7d": int, "spike_ratio": float}}
        current = last data point, avg_7d = mean of all data points,
        spike_ratio = current / avg_7d (or 0.0 if avg is zero).
    """
    if len(keywords) > PYTRENDS_BATCH_SIZE:
        raise ValueError(
            f"pytrends accepts at most {PYTRENDS_BATCH_SIZE} keywords per request, "
            f"got {len(keywords)}"
        )

    pt = pytrends_instance or _build_pytrends()
    results: dict = {}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            pt.build_payload(keywords, timeframe=timeframe)
            df = pt.interest_over_time()
            break
        except Exception as exc:
            # Catch 429 / TooManyRequestsError and general failures
            err_str = str(exc).lower()
            is_rate_limit = "429" in err_str or "too many" in err_str
            if is_rate_limit and attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF * attempt
                logger.warning(
                    "Google Trends rate limited (attempt %d/%d), "
                    "backing off %.0fs: %s",
                    attempt, MAX_RETRIES, wait, exc,
                )
                time.sleep(wait)
                continue
            logger.error(
                "Google Trends query failed after %d attempts: %s",
                attempt, exc,
            )
            # Return empty results for all keywords
            for kw in keywords:
                results[kw] = {"current": 0, "avg_7d": 0, "spike_ratio": 0.0}
            return results

    if df is None or df.empty:
        logger.warning("Google Trends returned empty data for keywords: %s", keywords)
        for kw in keywords:
            results[kw] = {"current": 0, "avg_7d": 0, "spike_ratio": 0.0}
        return results

    # Drop the "isPartial" column if present
    if "isPartial" in df.columns:
        df = df.drop(columns=["isPartial"])

    for kw in keywords:
        if kw not in df.columns:
            results[kw] = {"current": 0, "avg_7d": 0, "spike_ratio": 0.0}
            continue

        series = df[kw]
        current = int(series.iloc[-1]) if len(series) > 0 else 0
        avg_7d = int(series.mean()) if len(series) > 0 else 0
        spike_ratio = round(current / avg_7d, 2) if avg_7d > 0 else 0.0

        results[kw] = {
            "current": current,
            "avg_7d": avg_7d,
            "spike_ratio": spike_ratio,
        }

    return results


def detect_spikes(
    threshold: float = 2.0,
    keyword_groups: Optional[dict[str, list[str]]] = None,
    rate_limit: float = RATE_LIMIT_DELAY,
) -> list[dict]:
    """Check all keyword groups for interest spikes.

    Parameters
    ----------
    threshold : float
        Spike ratio threshold. Keywords with current / avg_7d > threshold
        are flagged.
    keyword_groups : dict, optional
        Override default KEYWORD_GROUPS.
    rate_limit : float
        Seconds to sleep between pytrends batches.

    Returns
    -------
    list[dict]
        Each dict has: keyword_group, keyword, spike_ratio, current_interest
        Only keywords exceeding the threshold are included.
    """
    groups = keyword_groups or KEYWORD_GROUPS
    spikes: list[dict] = []
    pt = _build_pytrends()

    # Flatten all keywords into batches of 5, tracking which group each belongs to
    keyword_to_group: dict[str, str] = {}
    all_keywords: list[str] = []
    for group_name, kw_list in groups.items():
        for kw in kw_list:
            keyword_to_group[kw] = group_name
            all_keywords.append(kw)

    # Process in batches of PYTRENDS_BATCH_SIZE
    batches = [
        all_keywords[i : i + PYTRENDS_BATCH_SIZE]
        for i in range(0, len(all_keywords), PYTRENDS_BATCH_SIZE)
    ]

    for idx, batch in enumerate(batches):
        logger.info(
            "Google Trends batch %d/%d: %s",
            idx + 1, len(batches), batch,
        )

        results = check_keyword_spikes(batch, pytrends_instance=pt)

        for kw, stats in results.items():
            if stats["spike_ratio"] > threshold:
                spikes.append({
                    "keyword_group": keyword_to_group.get(kw, "unknown"),
                    "keyword": kw,
                    "spike_ratio": stats["spike_ratio"],
                    "current_interest": stats["current"],
                })

        # Rate limit between batches (skip after last batch)
        if idx < len(batches) - 1:
            time.sleep(rate_limit)

    # Sort by spike_ratio descending
    spikes.sort(key=lambda x: x["spike_ratio"], reverse=True)
    logger.info(
        "Google Trends: checked %d keywords across %d groups -> %d spikes (threshold=%.1f)",
        len(all_keywords), len(groups), len(spikes), threshold,
    )
    return spikes


# ---------------------------------------------------------------------------
# CLI test mode
# ---------------------------------------------------------------------------

def _test_mode():
    """Run spike detection once and print results."""
    import json

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    print(f"\n{'='*80}")
    print("Google Trends Spike Detector — Test Run")
    print(f"{'='*80}\n")

    print("Checking all keyword groups (this may take a minute)...\n")
    spikes = detect_spikes(threshold=2.0)

    if spikes:
        print(f"SPIKES DETECTED: {len(spikes)}\n")
        for spike in spikes:
            print(
                f"  [{spike['keyword_group']}] \"{spike['keyword']}\" "
                f"— ratio {spike['spike_ratio']:.1f}x, "
                f"current interest {spike['current_interest']}"
            )
        print()
    else:
        print("No spikes detected above 2.0x threshold.\n")

    # Also run a single batch for verbose output
    print("--- Detailed output for market_crisis keywords ---")
    detail = check_keyword_spikes(KEYWORD_GROUPS["market_crisis"])
    for kw, stats in detail.items():
        print(f"  {kw}: current={stats['current']}, avg_7d={stats['avg_7d']}, ratio={stats['spike_ratio']}")

    print(f"\n--- Spikes JSON ({len(spikes)} items) ---")
    print(json.dumps(spikes, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    import sys
    if "--test" in sys.argv:
        _test_mode()
    else:
        print("Usage: python -m forge.atlas.sources.gtrends_monitor --test")
        sys.exit(1)
