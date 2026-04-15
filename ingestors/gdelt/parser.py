"""GEON GDELT response parser.

Parses raw responses from the GDELT DOC and GEO APIs into structured dicts
ready for Elasticsearch indexation.
"""

from __future__ import annotations

import csv
import hashlib
import io
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
    # Protest (14x)
    "140": "Protest, not specified",
    "141": "Demonstrate or rally",
    "142": "Conduct hunger strike",
    "143": "Conduct strike or boycott",
    "144": "Obstruct passage or block",
    "145": "Protest violently / riot",
    # Exhibit military posture (15x)
    "150": "Exhibit military posture, not specified",
    "151": "Increase police alert status",
    "152": "Increase military alert status",
    "153": "Mobilize or increase armed forces",
    "154": "Fortify, not specified",
    "155": "Increase military buildup",
    # Reduce relations / sanctions (16x)
    "160": "Reduce relations, not specified",
    "161": "Reduce or break diplomatic relations",
    "162": "Reduce or stop material aid",
    "163": "Impose embargo, boycott, or sanctions",
    "164": "Halt negotiations",
    "165": "Halt mediation",
    "166": "Expel or withdraw",
    # Coerce (17x)
    "170": "Coerce, not specified",
    "171": "Seize or damage property",
    "172": "Impose administrative sanctions",
    "173": "Arrest, detain, or charge with legal action",
    "174": "Expel or deport individuals",
    "175": "Use tactics of violent repression",
    # Assault (18x)
    "180": "Use unconventional violence, not specified",
    "181": "Abduct, hijack, or take hostage",
    "182": "Physically assault",
    "183": "Conduct bombing or explosion",
    "184": "Use as combatants",
    "185": "Attempt to assassinate",
    "186": "Assassinate",
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
# GDELT v2 Events Export CSV definitions
# https://www.gdeltproject.org/data/documentation/GDELT-Event_Codebook-V2.0.pdf
# ---------------------------------------------------------------------------

GDELT_CSV_COLUMNS: list[str] = [
    "GlobalEventID", "Day", "MonthYear", "Year", "FractionDate",
    "Actor1Code", "Actor1Name", "Actor1CountryCode", "Actor1KnownGroupCode",
    "Actor1EthnicCode", "Actor1Religion1Code", "Actor1Religion2Code",
    "Actor1Type1Code", "Actor1Type2Code", "Actor1Type3Code",
    "Actor2Code", "Actor2Name", "Actor2CountryCode", "Actor2KnownGroupCode",
    "Actor2EthnicCode", "Actor2Religion1Code", "Actor2Religion2Code",
    "Actor2Type1Code", "Actor2Type2Code", "Actor2Type3Code",
    "IsRootEvent", "EventCode", "EventBaseCode", "EventRootCode", "QuadClass",
    "GoldsteinScale", "NumMentions", "NumSources", "NumArticles", "AvgTone",
    "Actor1Geo_Type", "Actor1Geo_FullName", "Actor1Geo_CountryCode",
    "Actor1Geo_ADM1Code", "Actor1Geo_ADM2Code", "Actor1Geo_Lat",
    "Actor1Geo_Long", "Actor1Geo_FeatureID",
    "Actor2Geo_Type", "Actor2Geo_FullName", "Actor2Geo_CountryCode",
    "Actor2Geo_ADM1Code", "Actor2Geo_ADM2Code", "Actor2Geo_Lat",
    "Actor2Geo_Long", "Actor2Geo_FeatureID",
    "ActionGeo_Type", "ActionGeo_FullName", "ActionGeo_CountryCode",
    "ActionGeo_ADM1Code", "ActionGeo_ADM2Code", "ActionGeo_Lat",
    "ActionGeo_Long", "ActionGeo_FeatureID",
    "DATEADDED", "SOURCEURL",
]

# 3-letter CAMEO/ISO 3166-1 alpha-3 country codes used in Actor*CountryCode.
COUNTRY_CODE_TO_NAME: dict[str, str] = {
    "AFG": "AFGHANISTAN", "ALB": "ALBANIA", "DZA": "ALGERIA",
    "AGO": "ANGOLA", "ARG": "ARGENTINA", "ARM": "ARMENIA",
    "AUS": "AUSTRALIA", "AUT": "AUSTRIA", "AZE": "AZERBAIJAN",
    "BHR": "BAHRAIN", "BGD": "BANGLADESH", "BLR": "BELARUS",
    "BEL": "BELGIUM", "BEN": "BENIN", "BTN": "BHUTAN",
    "BOL": "BOLIVIA", "BIH": "BOSNIA AND HERZEGOVINA", "BWA": "BOTSWANA",
    "BRA": "BRAZIL", "BRN": "BRUNEI", "BGR": "BULGARIA",
    "BFA": "BURKINA FASO", "BDI": "BURUNDI", "KHM": "CAMBODIA",
    "CMR": "CAMEROON", "CAN": "CANADA", "CAF": "CENTRAL AFRICAN REPUBLIC",
    "TCD": "CHAD", "CHL": "CHILE", "CHN": "CHINA",
    "COL": "COLOMBIA", "COD": "CONGO (DRC)", "COG": "CONGO (REPUBLIC)",
    "CRI": "COSTA RICA", "CIV": "COTE D'IVOIRE", "HRV": "CROATIA",
    "CUB": "CUBA", "CYP": "CYPRUS", "CZE": "CZECH REPUBLIC",
    "DNK": "DENMARK", "DJI": "DJIBOUTI", "DOM": "DOMINICAN REPUBLIC",
    "ECU": "ECUADOR", "EGY": "EGYPT", "SLV": "EL SALVADOR",
    "GNQ": "EQUATORIAL GUINEA", "ERI": "ERITREA", "EST": "ESTONIA",
    "SWZ": "ESWATINI", "ETH": "ETHIOPIA", "FIN": "FINLAND",
    "FRA": "FRANCE", "GAB": "GABON", "GMB": "GAMBIA",
    "GEO": "GEORGIA", "DEU": "GERMANY", "GHA": "GHANA",
    "GRC": "GREECE", "GTM": "GUATEMALA", "GIN": "GUINEA",
    "GUY": "GUYANA", "HTI": "HAITI", "HND": "HONDURAS",
    "HUN": "HUNGARY", "ISL": "ICELAND", "IND": "INDIA",
    "IDN": "INDONESIA", "IRN": "IRAN", "IRQ": "IRAQ",
    "IRL": "IRELAND", "ISR": "ISRAEL", "ITA": "ITALY",
    "JAM": "JAMAICA", "JPN": "JAPAN", "JOR": "JORDAN",
    "KAZ": "KAZAKHSTAN", "KEN": "KENYA", "PRK": "NORTH KOREA",
    "KOR": "SOUTH KOREA", "KWT": "KUWAIT", "KGZ": "KYRGYZSTAN",
    "LAO": "LAOS", "LVA": "LATVIA", "LBN": "LEBANON",
    "LSO": "LESOTHO", "LBR": "LIBERIA", "LBY": "LIBYA",
    "LTU": "LITHUANIA", "LUX": "LUXEMBOURG", "MKD": "NORTH MACEDONIA",
    "MDG": "MADAGASCAR", "MWI": "MALAWI", "MYS": "MALAYSIA",
    "MLI": "MALI", "MLT": "MALTA", "MRT": "MAURITANIA",
    "MUS": "MAURITIUS", "MEX": "MEXICO", "MDA": "MOLDOVA",
    "MNG": "MONGOLIA", "MNE": "MONTENEGRO", "MAR": "MOROCCO",
    "MOZ": "MOZAMBIQUE", "MMR": "MYANMAR", "NAM": "NAMIBIA",
    "NPL": "NEPAL", "NLD": "NETHERLANDS", "NZL": "NEW ZEALAND",
    "NIC": "NICARAGUA", "NER": "NIGER", "NGA": "NIGERIA",
    "NOR": "NORWAY", "OMN": "OMAN", "PAK": "PAKISTAN",
    "PAN": "PANAMA", "PNG": "PAPUA NEW GUINEA", "PRY": "PARAGUAY",
    "PER": "PERU", "PHL": "PHILIPPINES", "POL": "POLAND",
    "PRT": "PORTUGAL", "QAT": "QATAR", "ROU": "ROMANIA",
    "RUS": "RUSSIA", "RWA": "RWANDA", "SAU": "SAUDI ARABIA",
    "SEN": "SENEGAL", "SRB": "SERBIA", "SLE": "SIERRA LEONE",
    "SGP": "SINGAPORE", "SVK": "SLOVAKIA", "SVN": "SLOVENIA",
    "SOM": "SOMALIA", "ZAF": "SOUTH AFRICA", "SSD": "SOUTH SUDAN",
    "ESP": "SPAIN", "LKA": "SRI LANKA", "SDN": "SUDAN",
    "SUR": "SURINAME", "SWE": "SWEDEN", "CHE": "SWITZERLAND",
    "SYR": "SYRIA", "TWN": "TAIWAN", "TJK": "TAJIKISTAN",
    "TZA": "TANZANIA", "THA": "THAILAND", "TGO": "TOGO",
    "TTO": "TRINIDAD AND TOBAGO", "TUN": "TUNISIA", "TUR": "TURKEY",
    "TKM": "TURKMENISTAN", "UGA": "UGANDA", "UKR": "UKRAINE",
    "ARE": "UNITED ARAB EMIRATES", "GBR": "UNITED KINGDOM",
    "USA": "UNITED STATES", "URY": "URUGUAY", "UZB": "UZBEKISTAN",
    "VEN": "VENEZUELA", "VNM": "VIETNAM", "YEM": "YEMEN",
    "ZMB": "ZAMBIA", "ZWE": "ZIMBABWE", "PSE": "PALESTINE",
    "XKX": "KOSOVO",
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


def resolve_country_name(code: str) -> str:
    """Convert a CAMEO/ISO 3-letter country code to a readable name.

    Returns the uppercase code itself if no mapping is found (e.g. actor
    type codes like ``"GOV"`` or ``"MIL"``).
    """
    return COUNTRY_CODE_TO_NAME.get(code.strip().upper(), code.strip().upper()) if code else ""


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
    """Normalize a raw GDELT event into the GEON Elasticsearch document format.

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
        or raw_event.get("DATEADDED")
        or raw_event.get("dateadded")
        or raw_event.get("SQLDATE")
        or raw_event.get("Day")
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

    # --- Actors ---
    actor1_name = (raw_event.get("Actor1Name") or "").strip()
    actor1_country = source_country  # already resolved above
    actor1_type = (raw_event.get("Actor1Type1Code") or "").strip()
    actor2_name = (raw_event.get("Actor2Name") or "").strip()
    actor2_country = target_country
    actor2_type = (raw_event.get("Actor2Type1Code") or "").strip()
    actor1_geo = (raw_event.get("Actor1Geo_FullName") or "").strip()
    actor2_geo = (raw_event.get("Actor2Geo_FullName") or "").strip()

    # --- QuadClass ---
    quad_class = _safe_int(raw_event.get("QuadClass"), default=0)

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
    event_id = (
        raw_event.get("event_id")
        or raw_event.get("GlobalEventID")
        or _generate_event_id(raw_event)
    )

    has_geo = geo_lat != 0.0 or geo_lon != 0.0

    doc: dict[str, Any] = {
        "event_id": str(event_id),
        "date": event_date.isoformat(),
        "source_country": source_country.upper(),
        "target_country": target_country.upper(),
        "actor1_name": actor1_name,
        "actor1_country": actor1_country.upper(),
        "actor1_type": actor1_type,
        "actor1_geo": actor1_geo,
        "actor2_name": actor2_name,
        "actor2_country": actor2_country.upper(),
        "actor2_type": actor2_type,
        "actor2_geo": actor2_geo,
        "quad_class": quad_class,
        "cameo_code": cameo_code,
        "cameo_description": cameo_info["description"],
        "goldstein_scale": goldstein,
        "tone": tone,
        "num_articles": num_articles,
        "source_url": source_url,
        "themes": themes,
        "persons": persons,
        "organizations": organizations,
        "severity": severity,
        "ingested_at": datetime.now(tz=timezone.utc).isoformat(),
    }

    # Only include geo fields when coordinates are valid (0,0 = Gulf of
    # Guinea is almost always a GDELT geocoding miss, not a real event).
    if has_geo:
        doc["geo_lat"] = geo_lat
        doc["geo_lon"] = geo_lon
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

def parse_events_csv(csv_text: str) -> list[dict[str, Any]]:
    """Parse a GDELT v2 Events Export CSV into a list of raw event dicts.

    The Events Export is a tab-separated file with 61 columns and no header
    row.  Each row is a distinct CAMEO-coded event with full metadata
    including Goldstein scale, geolocation, actors, and tone.

    Actor country codes are resolved to human-readable names via
    :data:`COUNTRY_CODE_TO_NAME`.

    Args:
        csv_text: Raw CSV content (tab-delimited, UTF-8).

    Returns:
        List of raw event dicts keyed by :data:`GDELT_CSV_COLUMNS`.
    """
    events: list[dict[str, Any]] = []
    reader = csv.reader(io.StringIO(csv_text), delimiter="\t")
    for row in reader:
        if len(row) < len(GDELT_CSV_COLUMNS):
            continue
        event = dict(zip(GDELT_CSV_COLUMNS, row))
        # Resolve 3-letter country codes to readable names.
        for key in ("Actor1CountryCode", "Actor2CountryCode"):
            raw_code = event.get(key, "").strip()
            if raw_code:
                event[key] = resolve_country_name(raw_code)
        events.append(event)

    logger.info("Events CSV: parsed %d rows.", len(events))
    return events


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
