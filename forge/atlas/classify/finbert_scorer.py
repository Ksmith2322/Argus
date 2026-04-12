"""
FinBERT sentiment scorer for Atlas event classification.

Uses ProsusAI/finbert (HuggingFace) to classify financial text as
positive / negative / neutral with confidence scores, then maps
the output to Atlas's 0-1 severity scale.

Model is ~420 MB, downloaded on first run and cached by HuggingFace.
Runs on CPU (~50-100 ms per headline).  Degrades gracefully if
transformers or torch is not installed.
"""

import argparse
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_pipeline = None
_load_failed = False


def load_model():
    """Load the FinBERT pipeline (singleton).

    Returns the ``transformers`` text-classification pipeline, or *None*
    if the required packages are missing.
    """
    global _pipeline, _load_failed

    if _pipeline is not None:
        return _pipeline

    if _load_failed:
        return None

    try:
        from transformers import pipeline as hf_pipeline
    except ImportError:
        logger.warning(
            "transformers is not installed — FinBERT scorer unavailable. "
            "Install with: pip install transformers torch"
        )
        _load_failed = True
        return None

    try:
        _pipeline = hf_pipeline(
            "sentiment-analysis",
            model="ProsusAI/finbert",
            tokenizer="ProsusAI/finbert",
            top_k=None,
        )
        logger.info("FinBERT model loaded successfully")
        return _pipeline
    except Exception as exc:
        logger.warning("Failed to load FinBERT model: %s", exc)
        _load_failed = True
        return None


# ---------------------------------------------------------------------------
# Financial severity mapping
# ---------------------------------------------------------------------------

def _compute_financial_severity(
    label: str, positive: float, negative: float, neutral: float,
) -> float:
    """Map FinBERT scores to Atlas 0-1 severity scale.

    Higher severity = more market-moving.
    """
    if label == "negative":
        if negative > 0.7:
            # High-confidence negative: 0.5–1.0
            return round(0.5 + negative * 0.5, 4)
        else:
            # Medium-confidence negative: 0.3–0.6
            return round(0.3 + negative * 0.3, 4)
    else:
        # Positive or neutral: low severity
        return round(max(0.1, 1.0 - positive) * 0.3, 4)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_sentiment(text: str) -> Optional[dict]:
    """Score a single headline / text with FinBERT.

    Returns
    -------
    dict or None
        Keys: label, score, positive, negative, neutral, financial_severity.
        *None* if the model is unavailable.
    """
    pipe = load_model()
    if pipe is None:
        return None

    results = pipe(text)
    # results is [[{label, score}, ...]] when return_all_scores=True
    scores_list = results[0]

    score_map: dict[str, float] = {}
    for entry in scores_list:
        score_map[entry["label"].lower()] = round(entry["score"], 4)

    positive = score_map.get("positive", 0.0)
    negative = score_map.get("negative", 0.0)
    neutral = score_map.get("neutral", 0.0)

    # Top label
    top = max(scores_list, key=lambda x: x["score"])
    label = top["label"].lower()
    confidence = round(top["score"], 4)

    severity = _compute_financial_severity(label, positive, negative, neutral)

    return {
        "label": label,
        "score": confidence,
        "positive": positive,
        "negative": negative,
        "neutral": neutral,
        "financial_severity": severity,
    }


def score_batch(texts: list[str]) -> list[Optional[dict]]:
    """Score multiple texts efficiently in batches of 16.

    Returns
    -------
    list[dict | None]
        One result per input text. *None* entries if the model is
        unavailable.
    """
    pipe = load_model()
    if pipe is None:
        return [None] * len(texts)

    batch_size = 16
    all_results: list[Optional[dict]] = []

    for start in range(0, len(texts), batch_size):
        chunk = texts[start : start + batch_size]
        batch_out = pipe(chunk)  # list of list-of-dicts

        for scores_list in batch_out:
            score_map: dict[str, float] = {}
            for entry in scores_list:
                score_map[entry["label"].lower()] = round(entry["score"], 4)

            positive = score_map.get("positive", 0.0)
            negative = score_map.get("negative", 0.0)
            neutral = score_map.get("neutral", 0.0)

            top = max(scores_list, key=lambda x: x["score"])
            label = top["label"].lower()
            confidence = round(top["score"], 4)

            severity = _compute_financial_severity(
                label, positive, negative, neutral,
            )

            all_results.append({
                "label": label,
                "score": confidence,
                "positive": positive,
                "negative": negative,
                "neutral": neutral,
                "financial_severity": severity,
            })

    return all_results


# ---------------------------------------------------------------------------
# Blending helper
# ---------------------------------------------------------------------------

def enhance_severity(
    keyword_severity: float,
    finbert_severity: float,
    weight: float = 0.3,
) -> float:
    """Blend keyword-based severity with FinBERT severity.

    Default mix: 70 % keyword + 30 % FinBERT.  Result is clamped to [0, 1].
    """
    blended = keyword_severity * (1.0 - weight) + finbert_severity * weight
    return round(max(0.0, min(1.0, blended)), 4)


# ---------------------------------------------------------------------------
# CLI test mode
# ---------------------------------------------------------------------------

_TEST_HEADLINES = [
    "Fed cuts rates by 50bp in emergency move as markets crash",
    "Markets rally on strong jobs data, unemployment falls to 3.4%",
    "Trump announces 25% tariffs on all Chinese imports effective immediately",
    "Ceasefire reached in Ukraine conflict, markets cautiously optimistic",
    "Silicon Valley Bank collapses in largest bank failure since 2008",
    "Oil prices stable as OPEC maintains current production levels",
]


def _run_test() -> None:
    """Test FinBERT scorer against sample headlines."""
    print("=" * 72)
    print("FinBERT Sentiment Scorer — Test Mode")
    print("=" * 72)

    pipe = load_model()
    if pipe is None:
        print("\n[ERROR] Model could not be loaded. Install dependencies:")
        print("  pip install transformers torch")
        return

    print(f"\nScoring {len(_TEST_HEADLINES)} headlines ...\n")

    results = score_batch(_TEST_HEADLINES)

    for headline, result in zip(_TEST_HEADLINES, results):
        if result is None:
            print(f"  {headline}")
            print("    -> scoring failed\n")
            continue

        print(f"  \"{headline}\"")
        print(
            f"    label={result['label']:>8s}  "
            f"conf={result['score']:.3f}  "
            f"pos={result['positive']:.3f}  "
            f"neg={result['negative']:.3f}  "
            f"neu={result['neutral']:.3f}  "
            f"severity={result['financial_severity']:.3f}"
        )
        print()

    # Quick enhance_severity demo
    print("-" * 72)
    print("enhance_severity demo (keyword=0.6, finbert=0.85, weight=0.3):")
    blended = enhance_severity(0.6, 0.85, 0.3)
    print(f"  blended severity = {blended}")
    print("=" * 72)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FinBERT sentiment scorer")
    parser.add_argument(
        "--test", action="store_true", help="Run test against sample headlines",
    )
    args = parser.parse_args()

    if args.test:
        logging.basicConfig(level=logging.INFO)
        _run_test()
    else:
        parser.print_help()
