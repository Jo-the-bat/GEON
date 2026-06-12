"""GEON canonical country dimension.

Every ingestor writes country values into Elasticsearch, but the upstream
sources disagree on naming: GDELT resolves CAMEO/ISO codes to uppercase
English ("RUSSIA"), ACLED returns mixed-case API names ("Russia"),
OFAC/EU/UN sanctions use free text, Cloudflare Radar uses ISO codes,
OpenCTI uses STIX location names ("Russian Federation").  Because the
correlation engine and the risk-score calculator join sources with exact
``term`` queries on ``keyword`` fields, any divergence silently breaks the
join (e.g. the ACLED factor of the risk score matched nothing as long as
ACLED docs stored "Russia" while the calculator queried "RUSSIA").

This module is the single source of truth: the canonical form is the
GDELT-style uppercase English name (e.g. ``"RUSSIA"``, ``"UNITED STATES"``,
``"COTE D'IVOIRE"``).  All ingestors MUST pass country values through
:func:`normalize_country` before indexing, and correlation rules should
normalize values read back from documents before using them in queries.

Usage::

    from common.countries import normalize_country, normalize_countries

    normalize_country("Russian Federation")   # -> "RUSSIA"
    normalize_country("Côte d'Ivoire")        # -> "COTE D'IVOIRE"
    normalize_country("RU")                   # -> "RUSSIA"  (ISO2)
    normalize_country("RUS")                  # -> "RUSSIA"  (ISO3)
    normalize_country("Burma")                # -> "MYANMAR"
    normalize_country("Atlantis")             # -> "ATLANTIS" (fallback: uppercased)
"""

from __future__ import annotations

import re
import unicodedata

# ---------------------------------------------------------------------------
# Canonical reference: CAMEO/ISO 3166-1 alpha-3 -> canonical uppercase name.
# This is the authoritative table (originally in gdelt/parser.py, which now
# imports it from here).
# ---------------------------------------------------------------------------
ISO3_TO_COUNTRY: dict[str, str] = {
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
    # UN members absent from the original GDELT table (previously these
    # surfaced as raw ISO3 codes in indexed documents).
    "AND": "ANDORRA", "ATG": "ANTIGUA AND BARBUDA", "BHS": "BAHAMAS",
    "BRB": "BARBADOS", "BLZ": "BELIZE", "CPV": "CAPE VERDE",
    "COM": "COMOROS", "DMA": "DOMINICA", "FJI": "FIJI",
    "GRD": "GRENADA", "GNB": "GUINEA-BISSAU", "KIR": "KIRIBATI",
    "LIE": "LIECHTENSTEIN", "MDV": "MALDIVES", "MHL": "MARSHALL ISLANDS",
    "FSM": "MICRONESIA", "MCO": "MONACO", "NRU": "NAURU",
    "PLW": "PALAU", "KNA": "SAINT KITTS AND NEVIS", "LCA": "SAINT LUCIA",
    "VCT": "SAINT VINCENT AND THE GRENADINES", "WSM": "SAMOA",
    "SMR": "SAN MARINO", "STP": "SAO TOME AND PRINCIPE",
    "SLB": "SOLOMON ISLANDS", "TLS": "EAST TIMOR", "TON": "TONGA",
    "TUV": "TUVALU", "VUT": "VANUATU", "VAT": "VATICAN",
    "SYC": "SEYCHELLES",
}

# ISO 3166-1 alpha-2 -> alpha-3 (Cloudflare Radar and several APIs use alpha-2).
ISO2_TO_ISO3: dict[str, str] = {
    "AF": "AFG", "AL": "ALB", "DZ": "DZA", "AO": "AGO", "AR": "ARG",
    "AM": "ARM", "AU": "AUS", "AT": "AUT", "AZ": "AZE", "BH": "BHR",
    "BD": "BGD", "BY": "BLR", "BE": "BEL", "BJ": "BEN", "BT": "BTN",
    "BO": "BOL", "BA": "BIH", "BW": "BWA", "BR": "BRA", "BN": "BRN",
    "BG": "BGR", "BF": "BFA", "BI": "BDI", "KH": "KHM", "CM": "CMR",
    "CA": "CAN", "CF": "CAF", "TD": "TCD", "CL": "CHL", "CN": "CHN",
    "CO": "COL", "CD": "COD", "CG": "COG", "CR": "CRI", "CI": "CIV",
    "HR": "HRV", "CU": "CUB", "CY": "CYP", "CZ": "CZE", "DK": "DNK",
    "DJ": "DJI", "DO": "DOM", "EC": "ECU", "EG": "EGY", "SV": "SLV",
    "GQ": "GNQ", "ER": "ERI", "EE": "EST", "SZ": "SWZ", "ET": "ETH",
    "FI": "FIN", "FR": "FRA", "GA": "GAB", "GM": "GMB", "GE": "GEO",
    "DE": "DEU", "GH": "GHA", "GR": "GRC", "GT": "GTM", "GN": "GIN",
    "GY": "GUY", "HT": "HTI", "HN": "HND", "HU": "HUN", "IS": "ISL",
    "IN": "IND", "ID": "IDN", "IR": "IRN", "IQ": "IRQ", "IE": "IRL",
    "IL": "ISR", "IT": "ITA", "JM": "JAM", "JP": "JPN", "JO": "JOR",
    "KZ": "KAZ", "KE": "KEN", "KP": "PRK", "KR": "KOR", "KW": "KWT",
    "KG": "KGZ", "LA": "LAO", "LV": "LVA", "LB": "LBN", "LS": "LSO",
    "LR": "LBR", "LY": "LBY", "LT": "LTU", "LU": "LUX", "MK": "MKD",
    "MG": "MDG", "MW": "MWI", "MY": "MYS", "ML": "MLI", "MT": "MLT",
    "MR": "MRT", "MU": "MUS", "MX": "MEX", "MD": "MDA", "MN": "MNG",
    "ME": "MNE", "MA": "MAR", "MZ": "MOZ", "MM": "MMR", "NA": "NAM",
    "NP": "NPL", "NL": "NLD", "NZ": "NZL", "NI": "NIC", "NE": "NER",
    "NG": "NGA", "NO": "NOR", "OM": "OMN", "PK": "PAK", "PA": "PAN",
    "PG": "PNG", "PY": "PRY", "PE": "PER", "PH": "PHL", "PL": "POL",
    "PT": "PRT", "QA": "QAT", "RO": "ROU", "RU": "RUS", "RW": "RWA",
    "SA": "SAU", "SN": "SEN", "RS": "SRB", "SL": "SLE", "SG": "SGP",
    "SK": "SVK", "SI": "SVN", "SO": "SOM", "ZA": "ZAF", "SS": "SSD",
    "ES": "ESP", "LK": "LKA", "SD": "SDN", "SR": "SUR", "SE": "SWE",
    "CH": "CHE", "SY": "SYR", "TW": "TWN", "TJ": "TJK", "TZ": "TZA",
    "TH": "THA", "TG": "TGO", "TT": "TTO", "TN": "TUN", "TR": "TUR",
    "TM": "TKM", "UG": "UGA", "UA": "UKR", "AE": "ARE", "GB": "GBR",
    "US": "USA", "UY": "URY", "UZ": "UZB", "VE": "VEN", "VN": "VNM",
    "YE": "YEM", "ZM": "ZMB", "ZW": "ZWE", "PS": "PSE", "XK": "XKX",
    "AD": "AND", "AG": "ATG", "BS": "BHS", "BB": "BRB", "BZ": "BLZ",
    "CV": "CPV", "KM": "COM", "DM": "DMA", "FJ": "FJI", "GD": "GRD",
    "GW": "GNB", "KI": "KIR", "LI": "LIE", "MV": "MDV", "MH": "MHL",
    "FM": "FSM", "MC": "MCO", "NR": "NRU", "PW": "PLW", "KN": "KNA",
    "LC": "LCA", "VC": "VCT", "WS": "WSM", "SM": "SMR", "ST": "STP",
    "SB": "SLB", "TL": "TLS", "TO": "TON", "TV": "TUV", "VU": "VUT",
    "VA": "VAT", "SC": "SYC",
}

# ---------------------------------------------------------------------------
# Aliases: alternate spellings observed in the upstream sources (ACLED API
# names, OFAC/EU/UN sanctions text, OpenCTI STIX locations, Polymarket
# question extraction, SIPRI datasets).  Keys are pre-normalized with
# _normalize_key (lowercase, accents stripped, punctuation collapsed,
# leading "the " removed).
# ---------------------------------------------------------------------------
ALIASES: dict[str, str] = {
    # --- Russia / former USSR ---
    "russian federation": "RUSSIA",
    "ussr": "RUSSIA",
    "soviet union": "RUSSIA",
    # --- United States ---
    "united states of america": "UNITED STATES",
    "america": "UNITED STATES",
    # --- United Kingdom ---
    "united kingdom of great britain and northern ireland": "UNITED KINGDOM",
    "great britain": "UNITED KINGDOM",
    "britain": "UNITED KINGDOM",
    "uk": "UNITED KINGDOM",
    "england": "UNITED KINGDOM",
    # --- Koreas ---
    "korea north": "NORTH KOREA",
    "north korea dprk": "NORTH KOREA",
    "democratic peoples republic of korea": "NORTH KOREA",
    "korea democratic peoples republic of": "NORTH KOREA",
    "dprk": "NORTH KOREA",
    "korea south": "SOUTH KOREA",
    "republic of korea": "SOUTH KOREA",
    "korea republic of": "SOUTH KOREA",
    "korea": "SOUTH KOREA",
    # --- China / Taiwan ---
    "peoples republic of china": "CHINA",
    "china peoples republic of": "CHINA",
    "prc": "CHINA",
    "mainland china": "CHINA",
    "taiwan province of china": "TAIWAN",
    "republic of china": "TAIWAN",
    "chinese taipei": "TAIWAN",
    # --- Congos ---
    "democratic republic of congo": "CONGO (DRC)",
    "democratic republic of the congo": "CONGO (DRC)",
    "congo democratic republic of the": "CONGO (DRC)",
    "congo democratic republic": "CONGO (DRC)",
    "dr congo": "CONGO (DRC)",
    "drc": "CONGO (DRC)",
    "congo kinshasa": "CONGO (DRC)",
    "zaire": "CONGO (DRC)",
    "republic of congo": "CONGO (REPUBLIC)",
    "republic of the congo": "CONGO (REPUBLIC)",
    "congo republic of": "CONGO (REPUBLIC)",
    "congo brazzaville": "CONGO (REPUBLIC)",
    "congo": "CONGO (REPUBLIC)",
    # --- Cote d'Ivoire ---
    "ivory coast": "COTE D'IVOIRE",
    "cote divoire": "COTE D'IVOIRE",
    "cote d ivoire": "COTE D'IVOIRE",
    # --- Renamed countries ---
    "burma": "MYANMAR",
    "myanmar burma": "MYANMAR",
    "czechia": "CZECH REPUBLIC",
    "turkiye": "TURKEY",
    "republic of turkiye": "TURKEY",
    "swaziland": "ESWATINI",
    "eswatini swaziland": "ESWATINI",
    "macedonia": "NORTH MACEDONIA",
    "republic of north macedonia": "NORTH MACEDONIA",
    "former yugoslav republic of macedonia": "NORTH MACEDONIA",
    "fyrom": "NORTH MACEDONIA",
    # --- Official long forms (UN/ISO style, used by sanctions lists & STIX) ---
    "syrian arab republic": "SYRIA",
    "iran islamic republic of": "IRAN",
    "islamic republic of iran": "IRAN",
    "venezuela bolivarian republic of": "VENEZUELA",
    "bolivarian republic of venezuela": "VENEZUELA",
    "bolivia plurinational state of": "BOLIVIA",
    "plurinational state of bolivia": "BOLIVIA",
    "tanzania united republic of": "TANZANIA",
    "united republic of tanzania": "TANZANIA",
    "moldova republic of": "MOLDOVA",
    "republic of moldova": "MOLDOVA",
    "lao peoples democratic republic": "LAOS",
    "lao pdr": "LAOS",
    "viet nam": "VIETNAM",
    "brunei darussalam": "BRUNEI",
    "arab republic of egypt": "EGYPT",
    "egypt arab republic of": "EGYPT",
    "yemen republic of": "YEMEN",
    "republic of yemen": "YEMEN",
    "kingdom of saudi arabia": "SAUDI ARABIA",
    "slovak republic": "SLOVAKIA",
    "kyrgyz republic": "KYRGYZSTAN",
    "republic of ireland": "IRELAND",
    "russian fed": "RUSSIA",
    # --- Palestine ---
    "palestinian territories": "PALESTINE",
    "palestinian territory": "PALESTINE",
    "occupied palestinian territory": "PALESTINE",
    "state of palestine": "PALESTINE",
    "palestine state of": "PALESTINE",
    "west bank": "PALESTINE",
    "gaza": "PALESTINE",
    "gaza strip": "PALESTINE",
    # --- CAMEO legacy country codes (GDELT uses a few non-ISO3 codes) ---
    "kos": "KOSOVO",
    "rom": "ROMANIA",
    "tmp": "EAST TIMOR",
    "zar": "CONGO (DRC)",
    "mtn": "MONTENEGRO",
    "wsb": "PALESTINE",
    "gzs": "PALESTINE",
    # --- Misc spellings ---
    "bosnia": "BOSNIA AND HERZEGOVINA",
    "bosnia herzegovina": "BOSNIA AND HERZEGOVINA",
    "burkina": "BURKINA FASO",
    "uae": "UNITED ARAB EMIRATES",
    "emirates": "UNITED ARAB EMIRATES",
    "holland": "NETHERLANDS",
    "kosovo republic of": "KOSOVO",
    "central african rep": "CENTRAL AFRICAN REPUBLIC",
    "gambia the": "GAMBIA",
    "timor leste": "EAST TIMOR",
    "cabo verde": "CAPE VERDE",
    "federated states of micronesia": "MICRONESIA",
    "micronesia federated states of": "MICRONESIA",
    "holy see": "VATICAN",
    "vatican city": "VATICAN",
    # --- Demonyms (the UN consolidated list stores NATIONALITY values like
    # "Iraqi" instead of country names) ---
    "afghan": "AFGHANISTAN",
    "algerian": "ALGERIA",
    "belarusian": "BELARUS",
    "burmese": "MYANMAR",
    "chinese": "CHINA",
    "congolese": "CONGO (DRC)",
    "egyptian": "EGYPT",
    "eritrean": "ERITREA",
    "ethiopian": "ETHIOPIA",
    "indian": "INDIA",
    "indonesian": "INDONESIA",
    "iranian": "IRAN",
    "iraqi": "IRAQ",
    "israeli": "ISRAEL",
    "jordanian": "JORDAN",
    "kazakh": "KAZAKHSTAN",
    "kenyan": "KENYA",
    "kuwaiti": "KUWAIT",
    "kyrgyz": "KYRGYZSTAN",
    "lebanese": "LEBANON",
    "libyan": "LIBYA",
    "malian": "MALI",
    "moroccan": "MOROCCO",
    "nigerian": "NIGERIA",
    "north korean": "NORTH KOREA",
    "pakistani": "PAKISTAN",
    "philippine": "PHILIPPINES",
    "filipino": "PHILIPPINES",
    "qatari": "QATAR",
    "russian": "RUSSIA",
    "rwandan": "RWANDA",
    "saudi arabian": "SAUDI ARABIA",
    "somali": "SOMALIA",
    "sudanese": "SUDAN",
    "syrian": "SYRIA",
    "tajik": "TAJIKISTAN",
    "tunisian": "TUNISIA",
    "turkish": "TURKEY",
    "ukrainian": "UKRAINE",
    "uzbek": "UZBEKISTAN",
    "yemeni": "YEMEN",
}

# Set of canonical names, for fast membership checks.
CANONICAL_COUNTRIES: frozenset[str] = frozenset(ISO3_TO_COUNTRY.values())

_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[.,;:!?\"()\[\]]")


def _normalize_key(value: str) -> str:
    """Normalize a string into the alias-lookup key form.

    Lowercases, strips accents, replaces ``&`` with ``and``, removes most
    punctuation (apostrophes and hyphens become spaces), collapses
    whitespace, and drops a leading ``"the "``.
    """
    text = unicodedata.normalize("NFKD", value)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold().replace("&", " and ")
    text = text.replace("'", "").replace("’", "").replace("-", " ")
    text = _PUNCT_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    if text.startswith("the "):
        text = text[4:]
    return text


def normalize_country(value: str | None) -> str:
    """Normalize any country representation to the GEON canonical form.

    Resolution order: canonical name (case-insensitive), ISO 3166-1
    alpha-3 code, alpha-2 code, alias table.  Unknown values fall back to
    the uppercased input so behaviour stays consistent with the historic
    GDELT parser (and unknown actor codes like ``"GOV"`` pass through).

    Args:
        value: Raw country value from any source (name, ISO code, free
            text).  ``None`` and empty strings return ``""``.

    Returns:
        Canonical uppercase country name, or the uppercased input when the
        value is not recognized.
    """
    if not value:
        return ""
    raw = str(value).strip()
    if not raw:
        return ""

    upper = raw.upper()
    if upper in CANONICAL_COUNTRIES:
        return upper
    if len(upper) == 3 and upper in ISO3_TO_COUNTRY:
        return ISO3_TO_COUNTRY[upper]
    if len(upper) == 2 and upper in ISO2_TO_ISO3:
        return ISO3_TO_COUNTRY[ISO2_TO_ISO3[upper]]

    key = _normalize_key(raw)
    if key in ALIASES:
        return ALIASES[key]

    # The key form may itself match a canonical name once punctuation
    # differences are removed (e.g. "Cote d'Ivoire" vs "COTE D'IVOIRE").
    canonical_by_key = _CANONICAL_BY_KEY.get(key)
    if canonical_by_key:
        return canonical_by_key

    return upper


def normalize_countries(values: list[str] | None) -> list[str]:
    """Normalize a list of country values, dropping empties and duplicates.

    Order is preserved (first occurrence wins).

    Args:
        values: Raw country values.

    Returns:
        List of canonical country names.
    """
    seen: set[str] = set()
    result: list[str] = []
    for value in values or []:
        name = normalize_country(value)
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return result


# Reverse lookup of canonical names by their normalized key form, so that
# punctuation/accent variants of canonical names resolve without aliases.
_CANONICAL_BY_KEY: dict[str, str] = {
    _normalize_key(name): name for name in CANONICAL_COUNTRIES
}
