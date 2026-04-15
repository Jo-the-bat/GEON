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

# Keywords that strongly indicate international geopolitics.
CONFLICT_KEYWORDS: set[str] = {
    "war", "invasion", "ceasefire", "nuclear", "sanctions",
    "treaty", "annexation", "coup", "missile", "drone strike",
    "occupation", "blockade", "genocide", "humanitarian crisis",
    "refugee", "espionage", "cyber attack", "assassination",
    "airstrike", "artillery", "naval", "submarine",
}

# Broader keywords — only valid when combined with >=2 countries or intl org.
CONTEXT_KEYWORDS: set[str] = {
    "military", "troops", "conflict", "peace", "diplomat",
    "embassy", "ambassador", "embargo", "alliance",
    "terrorism", "insurgency", "sovereignty", "border",
    "independence", "extradition", "intelligence",
}

# International organizations that signal geopolitics.
INTL_ORGS: set[str] = {
    "nato", "otan", "united nations", "un security council",
    "european union", "brics", "asean", "g7", "g20",
    "african union", "arab league", "iaea", "icc",
    "international court", "who", "imf", "world bank",
    "opec", "oecd",
}

# Patterns to EXCLUDE — US domestic politics, celebrities, etc.
EXCLUDE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"will .+ (be arrested|resign|say |tweet|endorse)", re.I),
    re.compile(r"will .+ win the 202\d (democrat|republican|gop)", re.I),
    re.compile(r"(gubernatorial|mayoral|senate race|house race)", re.I),
    re.compile(r"(super bowl|world series|nfl|nba|nhl|mlb|ufc)", re.I),
    re.compile(r"(bitcoin|ethereum|crypto|token|nft|defi)", re.I),
    re.compile(r"(movie|tv show|album|grammy|oscar|emmy|box office)", re.I),
]

# Tags to exclude.
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
    """Return True only for genuinely international geopolitical markets.

    Requirements (at least one must be true):
      1. Contains a strong conflict keyword (war, ceasefire, sanctions, etc.)
         AND involves at least 1 country or intl org.
      2. Involves >=2 distinct countries.
      3. Mentions an international organization (NATO, UN, EU, etc.).

    Excludes US domestic politics, celebrities, sports, crypto.
    """
    tags = {t.lower() for t in (market.get("tags") or [])}
    if tags & EXCLUDE_TAGS:
        return False

    question = (market.get("question") or market.get("title") or "").lower()
    description = (market.get("description") or "").lower()
    text = f"{question} {description}"

    # Explicit exclusion patterns.
    for pat in EXCLUDE_PATTERNS:
        if pat.search(text):
            return False

    # Extract countries and check for intl orgs.
    countries = extract_countries(text)
    has_intl_org = any(org in text for org in INTL_ORGS)

    # Path 1: strong conflict keyword + at least 1 country or intl org.
    words = set(text.split())
    has_conflict = bool(CONFLICT_KEYWORDS & {w.strip(".,!?") for w in words})
    # Also check multi-word conflict terms.
    if not has_conflict:
        for kw in CONFLICT_KEYWORDS:
            if " " in kw and kw in text:
                has_conflict = True
                break

    if has_conflict and (countries or has_intl_org):
        return True

    # Path 2: >=2 distinct countries mentioned.
    if len(countries) >= 2:
        return True

    # Path 3: international organization mentioned + context keyword.
    if has_intl_org:
        context_words = {w.strip(".,!?") for w in words}
        if CONTEXT_KEYWORDS & context_words:
            return True

    return False


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
        raw_prices = market.get("outcomePrices")
        if isinstance(raw_prices, str):
            try:
                raw_prices = json.loads(raw_prices)
            except (json.JSONDecodeError, TypeError):
                raw_prices = None
        if isinstance(raw_prices, list) and len(raw_prices) >= 2:
            yes_price = float(raw_prices[0])
            no_price = float(raw_prices[1])
        elif isinstance(raw_prices, list) and len(raw_prices) == 1:
            yes_price = float(raw_prices[0])
            no_price = 1.0 - yes_price

    countries = extract_countries(f"{question} {description}")
    all_keywords = CONFLICT_KEYWORDS | CONTEXT_KEYWORDS
    keywords = sorted(
        all_keywords & set(re.findall(r'\b\w+\b', f"{question} {description}".lower()))
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
