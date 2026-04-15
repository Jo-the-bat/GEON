"""GEON Polymarket parser.

Filters and normalizes Polymarket markets into geopolitical "cases".
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Keywords that indicate a geopolitical market.
GEO_KEYWORDS: set[str] = {
    "war", "invasion", "ceasefire", "nato", "nuclear", "sanctions",
    "treaty", "annexation", "coup", "election", "president",
    "prime minister", "military", "troops", "missile", "drone",
    "conflict", "peace", "ambassador", "diplomat", "embargo",
    "referendum", "independence", "occupation", "blockade",
    "parliament", "congress", "defense", "alliance", "coalition",
    "terrorism", "insurgency", "regime", "dictator", "revolution",
    "sovereignty", "border", "genocide", "humanitarian",
    "refugee", "asylum", "extradition", "espionage", "cyber",
    "intelligence", "surveillance", "assassination",
}

# Tags that indicate geopolitical content.
GEO_TAGS: set[str] = {
    "politics", "geopolitics", "war", "conflict", "elections",
    "sanctions", "world-politics", "international",
}

# Tags to exclude (sports, crypto, entertainment, etc.).
EXCLUDE_TAGS: set[str] = {
    "sports", "nfl", "nba", "mlb", "nhl", "soccer", "football",
    "cricket", "tennis", "golf", "boxing", "mma", "ufc",
    "crypto", "bitcoin", "ethereum", "defi", "nft",
    "entertainment", "movies", "music", "celebrities", "tv",
    "pop-culture", "reality-tv", "weather",
}

# Load country names for extraction.
_MAPPING_PATH = Path(__file__).resolve().parent.parent / "common" / "country_apt_mapping.json"
COUNTRY_NAMES: set[str] = set()
try:
    with _MAPPING_PATH.open() as f:
        for k in json.load(f):
            if k != "_comment":
                COUNTRY_NAMES.add(k.upper())
except Exception:
    pass

# Also add common variants.
COUNTRY_NAMES.update({
    "US", "USA", "UK", "EU", "NATO", "TAIWAN", "PALESTINE",
    "GAZA", "WEST BANK", "CRIMEA", "DONBAS", "KASHMIR",
    "TIBET", "HONG KONG", "KURDISTAN",
})

# Map short names to canonical names for matching.
SHORT_TO_CANONICAL: dict[str, str] = {
    "US": "UNITED STATES", "USA": "UNITED STATES",
    "UK": "UNITED KINGDOM", "EU": "EUROPE",
    "GAZA": "PALESTINE", "WEST BANK": "PALESTINE",
    "CRIMEA": "UKRAINE", "DONBAS": "UKRAINE",
    "KASHMIR": "INDIA", "TIBET": "CHINA",
    "HONG KONG": "CHINA", "KURDISTAN": "IRAQ",
}


def is_geopolitical(market: dict[str, Any]) -> bool:
    """Return True if a Polymarket market is geopolitical."""
    tags = {t.lower() for t in (market.get("tags") or [])}
    if tags & EXCLUDE_TAGS:
        return False
    if tags & GEO_TAGS:
        return True

    question = (market.get("question") or market.get("title") or "").lower()
    description = (market.get("description") or "").lower()
    text = f"{question} {description}"

    return bool(GEO_KEYWORDS & set(re.findall(r'\b\w+\b', text)))


def extract_countries(text: str) -> list[str]:
    """Extract country names from text using simple matching."""
    text_upper = text.upper()
    found: set[str] = set()
    for name in COUNTRY_NAMES:
        if re.search(r'\b' + re.escape(name) + r'\b', text_upper):
            canonical = SHORT_TO_CANONICAL.get(name, name)
            found.add(canonical)
    return sorted(found)


def normalize_market(market: dict[str, Any]) -> dict[str, Any]:
    """Convert a Polymarket API market object into a GEON case document."""
    market_id = market.get("condition_id") or market.get("id") or ""
    question = market.get("question") or market.get("title") or ""
    description = market.get("description") or ""

    # Price extraction — Gamma API format.
    yes_price = 0.0
    no_price = 0.0
    tokens = market.get("tokens") or []
    for token in tokens:
        outcome = (token.get("outcome") or "").lower()
        price = float(token.get("price") or 0)
        if outcome == "yes":
            yes_price = price
        elif outcome == "no":
            no_price = price
    if not tokens:
        yes_price = float(market.get("outcomePrices", [0, 0])[0] if market.get("outcomePrices") else 0)
        no_price = 1.0 - yes_price if yes_price else 0.0

    countries = extract_countries(f"{question} {description}")
    keywords = sorted(
        GEO_KEYWORDS & set(re.findall(r'\b\w+\b', f"{question} {description}".lower()))
    )

    end_date = market.get("end_date_iso") or market.get("endDate") or None
    created = market.get("created_at") or market.get("createdAt") or None
    volume = float(market.get("volume") or market.get("volumeNum") or 0)
    liquidity = float(market.get("liquidity") or market.get("liquidityNum") or 0)

    now = datetime.now(tz=timezone.utc).isoformat()
    active = market.get("active", True) and not market.get("closed", False)

    case_id = f"polymarket_{hashlib.sha256(market_id.encode()).hexdigest()[:16]}" if market_id else f"polymarket_{hashlib.sha256(question.encode()).hexdigest()[:16]}"

    return {
        "case_id": case_id,
        "question": question,
        "status": "active" if active else "resolved",
        "created_at": created,
        "end_date": end_date,
        "date": now,
        "outcome_yes_price": round(yes_price, 4),
        "outcome_no_price": round(no_price, 4),
        "volume": volume,
        "liquidity": liquidity,
        "price_history": [],
        "countries_involved": countries,
        "keywords": keywords[:20],
        "related_gdelt_events": 0,
        "related_correlations": 0,
        "related_apt_groups": [],
        "related_sanctions": 0,
        "trend": "stable",
        "price_change_24h": 0.0,
        "price_change_7d": 0.0,
        "category": "geopolitics",
        "source_url": f"https://polymarket.com/event/{market.get('slug', market_id)}",
        "ingested_at": now,
    }
