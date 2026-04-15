"""Cross-platform prediction market matcher.

Matches similar questions across Polymarket, Metaculus, and Manifold
to compute consensus scores and detect divergences.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Extract meaningful tokens for matching
_STOP_WORDS = {
    "will", "the", "be", "by", "in", "on", "of", "to", "and", "or",
    "a", "an", "is", "it", "for", "at", "this", "that", "with",
    "before", "after", "end", "year", "month", "2024", "2025", "2026",
    "2027", "2028", "2029", "2030",
}


def _tokenize(text: str) -> set[str]:
    """Extract meaningful lowercase tokens from a question."""
    tokens = set(re.findall(r"[a-z]+", text.lower()))
    return tokens - _STOP_WORDS


def similarity_score(q1: str, q2: str) -> float:
    """Compute Jaccard similarity between two questions.

    Returns:
        Similarity score between 0.0 and 1.0.
    """
    t1 = _tokenize(q1)
    t2 = _tokenize(q2)
    if not t1 or not t2:
        return 0.0
    intersection = t1 & t2
    union = t1 | t2
    return len(intersection) / len(union)


def find_matches(
    polymarket_cases: list[dict[str, Any]],
    external_markets: list[dict[str, Any]],
    threshold: float = 0.35,
) -> dict[str, list[dict[str, Any]]]:
    """Match Polymarket cases to external markets.

    Args:
        polymarket_cases: List of Polymarket case documents.
        external_markets: List of Metaculus/Manifold market documents.
        threshold: Minimum similarity score for a match.

    Returns:
        Dict mapping Polymarket case_id to list of matched externals.
    """
    matches: dict[str, list[dict[str, Any]]] = {}

    for pm_case in polymarket_cases:
        pm_q = pm_case.get("question", "")
        pm_id = pm_case.get("case_id", pm_case.get("_es_id", ""))
        pm_countries = set(pm_case.get("countries_involved", []))

        best_matches: list[tuple[float, dict[str, Any]]] = []

        for ext in external_markets:
            ext_q = ext.get("question", "")
            ext_countries = set(ext.get("countries_involved", []))

            # Country overlap boosts matching
            country_overlap = len(pm_countries & ext_countries) > 0 if pm_countries else False
            sim = similarity_score(pm_q, ext_q)

            # Boost if countries match
            if country_overlap:
                sim = min(1.0, sim + 0.15)

            if sim >= threshold:
                best_matches.append((sim, ext))

        if best_matches:
            best_matches.sort(key=lambda x: x[0], reverse=True)
            matches[pm_id] = [m[1] for m in best_matches[:3]]

    return matches


def compute_consensus(
    polymarket_price: float,
    matched_markets: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compute consensus score from matched platforms.

    Args:
        polymarket_price: Polymarket YES price (0-1).
        matched_markets: List of matched external market docs.

    Returns:
        Consensus dict with scores and divergence.
    """
    prices: list[tuple[str, float, float]] = [
        ("polymarket", polymarket_price, 1.0),  # weight 1.0 for base
    ]

    metaculus_price = None
    manifold_price = None

    for m in matched_markets:
        source = m.get("source", "")
        prob = m.get("probability", 0.0)
        if not prob or prob <= 0:
            continue
        volume = max(m.get("volume", 0.0), 1.0)
        if source == "metaculus":
            metaculus_price = prob
            prices.append(("metaculus", prob, 0.5))  # lower weight (no volume)
        elif source == "manifold":
            manifold_price = prob
            weight = min(volume / 1000, 1.0)  # scale by volume
            prices.append(("manifold", prob, max(weight, 0.3)))

    # Weighted average
    total_weight = sum(w for _, _, w in prices)
    consensus_score = sum(p * w for _, p, w in prices) / total_weight if total_weight > 0 else 0.0

    # Divergence (std dev of prices)
    all_prices = [p for _, p, _ in prices]
    if len(all_prices) >= 2:
        mean = sum(all_prices) / len(all_prices)
        variance = sum((p - mean) ** 2 for p in all_prices) / len(all_prices)
        divergence = round(math.sqrt(variance), 4)
    else:
        divergence = 0.0

    now = datetime.now(timezone.utc).isoformat()

    return {
        "polymarket_yes": round(polymarket_price, 4),
        "metaculus_median": round(metaculus_price, 4) if metaculus_price is not None else None,
        "manifold_yes": round(manifold_price, 4) if manifold_price is not None else None,
        "consensus_score": round(consensus_score, 4),
        "divergence": divergence,
        "platforms_count": len(prices),
        "last_updated": now,
    }
