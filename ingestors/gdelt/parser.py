"""HEGO GDELT response parser.

Parses raw responses from the GDELT DOC and GEO APIs into structured dicts
ready for Elasticsearch indexation.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CAMEO code reference
# https://www.gdeltproject.org/data/lookups/CAMEO.eventcodes.txt
# ---------------------------------------------------------------------------

CAMEO_CODES: dict[str, str] = {
    "01": "Make public statement",
    "02": "Appeal",
    "03": "Express intent to cooperate",
    "04": "Consult",
    "05": "Engage in diplomatic cooperation",
    "06": "Engage in material cooperation",
    "07": "Provide aid",
    "08": "Yield",
    "09": "Investigate",
    "10": "Demand",
    "11": "Disapprove",
    "12": "Reject",
    "13": "Threaten",
    "14": "Protest",
    "15": "Exhibit military posture",
    "16": "Reduce relations",
    "17": "Coerce",
    "18": "Assault",
    "19": "Fight",
    "20": "Engage in unconventional mass violence",
}

# Sub-codes for the categories we track most closely.
CAMEO_SUBCODES: dict[str, str] = {
    # Consult / military cooperation (04x)
    "040": "Consult, not specified",
    "041": "Discuss by telephone",
    "042": "Make a visit",
    "043": "Host a visit",
    "044": "Meet at a third location",
    "045": "Mediate",
    "046": "Engage in negotiation",
    # Diplomatic cooperation (05x)
    "050": "Engage in diplomatic cooperation, not specified",
    "051": "Praise or endorse",
    "052": "Defend verbally",
    "053": "Rally support on behalf of",
    "054": "Grant diplomatic recognition",
    "055": "Apologize",
    "056": "Forgive",
    "057": "Sign formal agreement",
    # Material cooperation (06x)
    "060": "Engage in material cooperation, not specified",
    "061": "Cooperate economically",
    "062": "Cooperate militarily",
    "063": "Engage in judicial cooperation",
    "064": "Share intelligence or information",
    # Threaten (13x)
    "130": "Threaten, not specified",
    "131": "Threaten non-force",
    "132": "Threaten with administrative sanctions",
    "133": "Threaten political dissent / repression",
    "134": "Threaten with political dissent",
    "135": "Threaten with military force",
    "136": "Threaten with weapons of mass destruction",
    "137": "Threaten to attack critical infrastructure",
    "138": "Threaten with cyber attack",
    "139": "Threaten unconventional violence",
    # Reduce relations / sanctions (16x)
    "160": "Reduce relations, not specified",
    "161": "Reduce or break diplomatic relations",
    "162": "Reduce or stop material aid",
    "163": "Impose embargo, boycott, or sanctions",
    "164": "Halt negotiations",
    "165": "Halt mediation",
    "166": "Expel or withdraw",
    # Fight (19x)
    "190": "Use conventional military force, not specified",
    "191": "Impose blockade / restrict movement",
    "192": "Occupy territory",
    "193": "Fight with small arms and light weapons",
    "194": "Fight with artillery and tanks",
    "195": "Employ aerial weapons",
    "196": "Violate ceasefire",
    # Unconventional mass violence (20x)
    "200": "Engage in mass expulsion",
    "201": "Engage in ethnic cleansing",
    "202": "Engage in mass killing",
    "203": "Use weapons of mass destruction",
    "204": "Engage in unconventional cyber attack",
}

# CAMEO codes that indicate conflict / tension — used for filtering.
RELEVANT_CAMEO_PREFIXES: set[str] = {
    "04",  # Military cooperation / consult
    "05",  # Diplomatic cooperation
    "06",  # Material cooperation
    "13",  # Threaten
    "14",  # Protest
    "15",  # Military posture
    "16",  # Reduce relations / sanctions
    "17",  # Coerce
    "18",  # Assault
    "19",  # Fight
    "20",  # Unconventional mass violence
}


# ---------------------------------------------------------------------------
# CAMEO helpers
# ---------------------------------------------------------------------------

def extract_cameo_info(cameo_code: str) -> dict[str, str]:
    """Map a CAMEO event code to its description and high-level category.

    Args:
        cameo_code: CAMEO event code string (e.g. ``"190"`` or ``"19"``).

    Returns:
        Dict with keys ``"code"``, ``"description"``, ``"category"``, and
        ``"category_description"``.
    """
    code = str(cameo_code).strip()
    category = code[:2] if len(code) >= 2 else code

    return {
        "code": code,
        "description": CAMEO_SUBCODES.get(code, CAMEO_CODES.get(category, "Unknown")),
        "category": category,
        "category_description": CAMEO_CODES.get(category, "Unknown"),
    }


# ---------------------------------------------------------------------------
# Severity calculation
# ---------------------------------------------------------------------------

def calculate_severity(
    goldstein_scale: float,
    num_articles: int,
    tone: float,
) -> str:
    """Compute a severity label from GDELT event metrics.

    The heuristic combines the Goldstein conflict-cooperation scale (range
    roughly -10 to +10, negative = conflict), article count (proxy for media
    salience), and average tone.

    Args:
        goldstein_scale: Goldstein scale value (negative = conflict).
        num_articles: Number of articles mentioning the event.
        tone: Average tone of coverage (negative = negative sentiment).

    Returns:
        One of ``"low"``, ``"medium"``, ``"high"``, ``"critical"``.
    """
    score = 0.0

    # Goldstein contribution (most weight).
    if goldstein_scale <= -9.0:
        score += 4.0
    elif goldstein_scale <= -7.0:
        score += 3.0
    elif goldstein_scale <= -5.0:
        score += 2.0
    elif goldstein_scale <= -2.0:
        score += 1.0

    # Media salience.
    if num_articles >= 100:
        score += 2.0
    elif num_articles >= 50:
        score += 1.5
    elif num_articles >= 20:
        score += 1.0
    elif num_articles >= 5:
        score += 0.5

    # Tone contribution.
    if tone <= -8.0:
        score += 2.0
    elif tone <= -5.0:
        score += 1.5
    elif tone <= -3.0:
        score += 1.0
    elif tone <= -1.0:
        score += 0.5

    if score >= 6.0:
        return "critical"
    if score >= 4.0:
        return "high"
    if score >= 2.0:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Event normalization
# ---------------------------------------------------------------------------

def _safe_float(value: Any, default: float = 0.0) -> float:
    """Convert *value* to float, returning *default* on failure."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    """Convert *value* to int, returning *default* on failure."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _generate_event_id(event: dict[str, Any]) -> str:
    """Generate a deterministic ID for a GDELT event.

    Uses a hash of date + URL + countries so that re-ingesting the same
    event produces the same ``_id`` in Elasticsearch (upsert semantics).
    """
    raw = (
        f"{event.get('date', '')}"
        f"{event.get('url', event.get('source_url', ''))}"
        f"{event.get('source_country', '')}"
        f"{event.get('target_country', '')}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def normalize_event(raw_event: dict[str, Any]) -> dict[str, Any]:
    """Normalize a raw GDELT event into the HEGO Elasticsearch document format.

    Handles both DOC API article objects and GEO API event objects with
    graceful fallbacks for missing fields.

    Args:
        raw_event: Raw event dict from a GDELT API response.

    Returns:
        Normalized document ready for indexation.
    """
    # --- Resolve date ---
    date_raw = (
        raw_event.get("seendate")
        or raw_event.get("dateadded")
        or raw_event.get("SQLDATE")
        or raw_event.get("date")
        or ""
    )
    try:
        if isinstance(date_raw, str) and len(date_raw) == 14:
            # GDELT format: YYYYMMDDHHmmSS
            event_date = datetime.strptime(date_raw, "%Y%m%d%H%M%S").replace(
                tzinfo=timezone.utc,
            )
        elif isinstance(date_raw, str) and len(date_raw) == 8:
            event_date = datetime.strptime(date_raw, "%Y%m%d").replace(
                tzinfo=timezone.utc,
            )
        else:
            event_date = datetime.fromisoformat(str(date_raw))
    except (ValueError, TypeError):
        event_date = datetime.now(tz=timezone.utc)

    # --- Countries ---
    source_country: str = (
        raw_event.get("source_country")
        or raw_event.get("Actor1CountryCode")
        or raw_event.get("sourcecountry")
        or ""
    )
    target_country: str = (
        raw_event.get("target_country")
        or raw_event.get("Actor2CountryCode")
        or raw_event.get("targetcountry")
        or ""
    )

    # --- CAMEO ---
    cameo_code: str = str(
        raw_event.get("cameo_code")
        or raw_event.get("EventCode")
        or raw_event.get("eventcode")
        or ""
    )
    cameo_info = extract_cameo_info(cameo_code)

    # --- Numeric fields ---
    goldstein = _safe_float(
        raw_event.get("goldstein_scale")
        or raw_event.get("GoldsteinScale")
        or raw_event.get("goldsteinscale"),
    )
    tone = _safe_float(
        raw_event.get("tone")
        or raw_event.get("AvgTone")
        or raw_event.get("avgtone"),
    )
    num_articles = _safe_int(
        raw_event.get("num_articles")
        or raw_event.get("NumArticles")
        or raw_event.get("numarticles"),
        default=1,
    )

    # --- Geo ---
    geo_lat = _safe_float(
        raw_event.get("geo_lat")
        or raw_event.get("ActionGeo_Lat")
        or raw_event.get("actiongeolat"),
    )
    geo_lon = _safe_float(
        raw_event.get("geo_lon")
        or raw_event.get("ActionGeo_Long")
        or raw_event.get("actiongeolong"),
    )

    # --- Text arrays ---
    themes = _coerce_list(raw_event.get("themes", []))
    persons = _coerce_list(raw_event.get("persons", []))
    organizations = _coerce_list(raw_event.get("organizations", []))

    # --- URL ---
    source_url: str = str(
        raw_event.get("url")
        or raw_event.get("source_url")
        or raw_event.get("SOURCEURL")
        or ""
    )

    # --- Severity ---
    severity = calculate_severity(goldstein, num_articles, tone)

    # --- Build document ---
    event_id = raw_event.get("event_id") or _generate_event_id(raw_event)

    doc: dict[str, Any] = {
        "event_id": str(event_id),
        "date": event_date.isoformat(),
        "source_country": source_country.upper(),
        "target_country": target_country.upper(),
        "cameo_code": cameo_code,
        "cameo_description": cameo_info["description"],
        "goldstein_scale": goldstein,
        "tone": tone,
        "num_articles": num_articles,
        "geo_lat": geo_lat,
        "geo_lon": geo_lon,
        "source_url": source_url,
        "themes": themes,
        "persons": persons,
        "organizations": organizations,
        "severity": severity,
        "ingested_at": datetime.now(tz=timezone.utc).isoformat(),
    }

    # Add geo_location for Kibana maps (only when coordinates are valid).
    if geo_lat != 0.0 or geo_lon != 0.0:
        doc["geo_location"] = {"lat": geo_lat, "lon": geo_lon}

    return doc


def _coerce_list(value: Any) -> list[str]:
    """Ensure *value* is a list of non-empty strings.

    Handles the case where the GDELT API returns a semicolon-delimited
    string instead of a list.
    """
    if isinstance(value, list):
        return [str(v).strip() for v in value if v]
    if isinstance(value, str) and value:
        return [s.strip() for s in value.split(";") if s.strip()]
    return []


# ---------------------------------------------------------------------------
# API response parsers
# ---------------------------------------------------------------------------

def parse_doc_api_response(response_json: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse a GDELT DOC API (v2) JSON response into a list of raw events.

    The DOC API returns an object with an ``"articles"`` array.  Each article
    has keys like ``url``, ``seendate``, ``title``, ``tone``, ``domain``,
    ``socialimage``, etc.

    This function extracts the articles and augments each one with any
    top-level metadata the API provides.

    Args:
        response_json: Decoded JSON body from the GDELT DOC API.

    Returns:
        List of raw event dicts (not yet normalized).
    """
    articles: list[dict[str, Any]] = response_json.get("articles", [])
    if not articles:
        logger.debug("DOC API response contains no articles.")
        return []

    logger.info("DOC API returned %d articles.", len(articles))
    return articles


def parse_geo_api_response(response_json: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse a GDELT GEO API (v2) JSON response into a list of raw events.

    The GEO API returns a GeoJSON FeatureCollection.  Each Feature has
    ``geometry`` (Point) and ``properties`` with event attributes.

    Args:
        response_json: Decoded JSON body from the GDELT GEO API.

    Returns:
        List of raw event dicts with lat/lon hoisted into the dict.
    """
    features: list[dict[str, Any]] = response_json.get("features", [])
    if not features:
        logger.debug("GEO API response contains no features.")
        return []

    events: list[dict[str, Any]] = []
    for feature in features:
        props = dict(feature.get("properties", {}))
        geometry = feature.get("geometry", {})
        coords = geometry.get("coordinates", [])
        if len(coords) >= 2:
            # GeoJSON is [lon, lat].
            props["geo_lon"] = coords[0]
            props["geo_lat"] = coords[1]
        events.append(props)

    logger.info("GEO API returned %d features.", len(events))
    return events
