"""Parser for Metaculus and Manifold Markets prediction data.

Filters for geopolitical markets and normalizes into a common schema.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Geopolitical keywords for filtering
GEO_KEYWORDS: set[str] = {
    "war", "ceasefire", "nuclear", "sanctions", "invasion", "nato",
    "military", "troops", "missile", "drone", "conflict", "territory",
    "annexation", "blockade", "embargo", "coup", "insurgency",
    "peacekeeping", "un security council", "arms", "defense",
    "sovereignty", "alliance", "treaty", "diplomacy", "geopolitical",
    "martial law", "drone strike", "air strike", "occupation",
}

# Exclusion patterns
EXCLUDE_PATTERNS: set[str] = {
    "super bowl", "nfl", "nba", "oscars", "grammy", "bitcoin",
    "ethereum", "crypto", "price of", "stock market", "s&p",
    "will openai", "will google", "tiktok ban",
}

# Country extraction (reuse simplified version)
_COUNTRY_RE = re.compile(
    r"\b(?:United States|Russia|China|Iran|Israel|Ukraine|North Korea|"
    r"India|Pakistan|Turkey|Saudi Arabia|Taiwan|Syria|Iraq|Yemen|"
    r"Lebanon|Afghanistan|Myanmar|Venezuela|Cuba|Libya|Somalia|"
    r"Sudan|South Korea|Japan|Germany|France|UK|United Kingdom|"
    r"NATO|EU|European Union|UN|BRICS|ASEAN|"
    r"Egypt|Ethiopia|Nigeria|South Africa|Brazil|Mexico|Colombia|"
    r"Philippines|Indonesia|Thailand|Poland|Romania|Canada|Australia)\b",
    re.IGNORECASE,
)

# Normalize short names
_COUNTRY_NORMALIZE: dict[str, str] = {
    "us": "UNITED STATES", "usa": "UNITED STATES", "uk": "UNITED KINGDOM",
    "eu": "EUROPEAN UNION",
}


def is_geopolitical(question: str) -> bool:
    """Check if a market question is geopolitical."""
    q = question.lower()
    if any(exc in q for exc in EXCLUDE_PATTERNS):
        return False
    return any(kw in q for kw in GEO_KEYWORDS) or len(_COUNTRY_RE.findall(question)) >= 2


def extract_countries(question: str) -> list[str]:
    """Extract country/org names from a question."""
    matches = _COUNTRY_RE.findall(question)
    normalized = set()
    for m in matches:
        n = _COUNTRY_NORMALIZE.get(m.lower(), m.upper())
        normalized.add(n)
    return sorted(normalized)


def normalize_manifold_market(market: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize a Manifold Markets market into common schema."""
    question = market.get("question", "")
    if not is_geopolitical(question):
        return None

    market_id = f"manifold-{market.get('id', '')}"
    probability = market.get("probability", 0.0)
    volume = market.get("volume", 0.0) or market.get("totalLiquidity", 0.0)
    close_time = market.get("closeTime")
    url = market.get("url", "")
    now = datetime.now(timezone.utc).isoformat()

    # Convert epoch ms to ISO
    close_iso = None
    if close_time and isinstance(close_time, (int, float)):
        close_iso = datetime.fromtimestamp(close_time / 1000, tz=timezone.utc).isoformat()

    is_resolved = market.get("isResolved", False)

    return {
        "market_id": market_id,
        "question": question,
        "source": "manifold",
        "source_url": url,
        "probability": round(probability, 4),
        "volume": round(volume, 2),
        "status": "resolved" if is_resolved else "active",
        "countries_involved": extract_countries(question),
        "close_time": close_iso,
        "date": now,
        "ingested_at": now,
    }


def normalize_metaculus_question(q: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize a Metaculus question into common schema."""
    title = q.get("title", "") or q.get("question", {}).get("title", "")
    if not title or not is_geopolitical(title):
        return None

    market_id = f"metaculus-{q.get('id', '')}"
    now = datetime.now(timezone.utc).isoformat()

    # Metaculus probability — varies by question type
    forecast = q.get("community_prediction", {})
    probability = forecast.get("full", {}).get("q2") or forecast.get("q2", 0.0)
    if not probability:
        # Try 'my_predictions' or 'aggregations'
        agg = q.get("aggregations", {}).get("recency_weighted", {})
        probability = agg.get("latest", {}).get("centers", [None])[0]
    probability = probability or 0.0

    close_time = q.get("close_time") or q.get("scheduled_close_time")
    url = f"https://www.metaculus.com/questions/{q.get('id', '')}/"

    status = "active"
    if q.get("resolution") is not None or q.get("actual_close_time"):
        status = "resolved"

    return {
        "market_id": market_id,
        "question": title,
        "source": "metaculus",
        "source_url": url,
        "probability": round(float(probability), 4) if probability else 0.0,
        "volume": 0.0,  # Metaculus doesn't have monetary volume
        "status": status,
        "countries_involved": extract_countries(title),
        "close_time": close_time,
        "date": now,
        "ingested_at": now,
    }
