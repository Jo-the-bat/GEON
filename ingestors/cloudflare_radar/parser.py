"""Parser for Cloudflare Radar outage annotations.

Normalizes raw API responses into the ``geon-outages`` document schema.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ISO-3166 alpha-2 → country name (common geopolitical actors).
_CC_TO_NAME: dict[str, str] = {
    "AF": "AFGHANISTAN", "AL": "ALBANIA", "DZ": "ALGERIA", "AO": "ANGOLA",
    "AR": "ARGENTINA", "AM": "ARMENIA", "AU": "AUSTRALIA", "AT": "AUSTRIA",
    "AZ": "AZERBAIJAN", "BH": "BAHRAIN", "BD": "BANGLADESH", "BY": "BELARUS",
    "BE": "BELGIUM", "BO": "BOLIVIA", "BA": "BOSNIA AND HERZEGOVINA",
    "BR": "BRAZIL", "BG": "BULGARIA", "MM": "MYANMAR", "KH": "CAMBODIA",
    "CM": "CAMEROON", "CA": "CANADA", "CF": "CENTRAL AFRICAN REPUBLIC",
    "TD": "CHAD", "CL": "CHILE", "CN": "CHINA", "CO": "COLOMBIA",
    "CD": "DEMOCRATIC REPUBLIC OF THE CONGO", "CG": "REPUBLIC OF THE CONGO",
    "CR": "COSTA RICA", "HR": "CROATIA", "CU": "CUBA", "CY": "CYPRUS",
    "CZ": "CZECH REPUBLIC", "DK": "DENMARK", "DJ": "DJIBOUTI",
    "DO": "DOMINICAN REPUBLIC", "EC": "ECUADOR", "EG": "EGYPT",
    "SV": "EL SALVADOR", "GQ": "EQUATORIAL GUINEA", "ER": "ERITREA",
    "EE": "ESTONIA", "ET": "ETHIOPIA", "FI": "FINLAND", "FR": "FRANCE",
    "GA": "GABON", "GE": "GEORGIA", "DE": "GERMANY", "GH": "GHANA",
    "GR": "GREECE", "GT": "GUATEMALA", "GN": "GUINEA", "HT": "HAITI",
    "HN": "HONDURAS", "HU": "HUNGARY", "IS": "ICELAND", "IN": "INDIA",
    "ID": "INDONESIA", "IR": "IRAN", "IQ": "IRAQ", "IE": "IRELAND",
    "IL": "ISRAEL", "IT": "ITALY", "CI": "IVORY COAST", "JM": "JAMAICA",
    "JP": "JAPAN", "JO": "JORDAN", "KZ": "KAZAKHSTAN", "KE": "KENYA",
    "KW": "KUWAIT", "KG": "KYRGYZSTAN", "LA": "LAOS", "LV": "LATVIA",
    "LB": "LEBANON", "LY": "LIBYA", "LT": "LITHUANIA", "LU": "LUXEMBOURG",
    "MK": "NORTH MACEDONIA", "MG": "MADAGASCAR", "MW": "MALAWI",
    "MY": "MALAYSIA", "ML": "MALI", "MR": "MAURITANIA", "MX": "MEXICO",
    "MD": "MOLDOVA", "MN": "MONGOLIA", "ME": "MONTENEGRO", "MA": "MOROCCO",
    "MZ": "MOZAMBIQUE", "NA": "NAMIBIA", "NP": "NEPAL", "NL": "NETHERLANDS",
    "NZ": "NEW ZEALAND", "NI": "NICARAGUA", "NE": "NIGER", "NG": "NIGERIA",
    "KP": "NORTH KOREA", "NO": "NORWAY", "OM": "OMAN", "PK": "PAKISTAN",
    "PS": "PALESTINE", "PA": "PANAMA", "PG": "PAPUA NEW GUINEA",
    "PY": "PARAGUAY", "PE": "PERU", "PH": "PHILIPPINES", "PL": "POLAND",
    "PT": "PORTUGAL", "QA": "QATAR", "RO": "ROMANIA", "RU": "RUSSIA",
    "RW": "RWANDA", "SA": "SAUDI ARABIA", "SN": "SENEGAL", "RS": "SERBIA",
    "SL": "SIERRA LEONE", "SG": "SINGAPORE", "SK": "SLOVAKIA",
    "SI": "SLOVENIA", "SO": "SOMALIA", "ZA": "SOUTH AFRICA",
    "KR": "SOUTH KOREA", "SS": "SOUTH SUDAN", "ES": "SPAIN", "LK": "SRI LANKA",
    "SD": "SUDAN", "SE": "SWEDEN", "CH": "SWITZERLAND", "SY": "SYRIA",
    "TW": "TAIWAN", "TJ": "TAJIKISTAN", "TZ": "TANZANIA", "TH": "THAILAND",
    "TG": "TOGO", "TN": "TUNISIA", "TR": "TURKEY", "TM": "TURKMENISTAN",
    "UG": "UGANDA", "UA": "UKRAINE", "AE": "UNITED ARAB EMIRATES",
    "GB": "UNITED KINGDOM", "US": "UNITED STATES", "UY": "URUGUAY",
    "UZ": "UZBEKISTAN", "VE": "VENEZUELA", "VN": "VIETNAM", "YE": "YEMEN",
    "ZM": "ZAMBIA", "ZW": "ZIMBABWE",
}


def resolve_country(code: str) -> str:
    """Resolve a 2-letter country code to an uppercase country name."""
    return _CC_TO_NAME.get(code.upper(), code.upper())


def classify_outage(annotation: dict[str, Any]) -> tuple[str, str, str]:
    """Derive type, scope, and severity from the raw annotation.

    Returns:
        Tuple of (type, scope, severity).
    """
    locations = annotation.get("locations", [])
    asns = annotation.get("asns", [])
    outage_type = annotation.get("outageType", annotation.get("type", ""))

    # Type classification
    if asns and not locations:
        otype = "asn-level"
    elif len(locations) == 1:
        otype = "country-level"
    elif len(locations) > 1:
        otype = "region"
    else:
        otype = outage_type or "unknown"

    # Scope
    scope_raw = annotation.get("scope", "")
    if scope_raw:
        scope = scope_raw
    elif otype == "country-level":
        scope = "national"
    elif otype == "region":
        scope = "regional"
    else:
        scope = "local"

    # Severity
    sev_raw = annotation.get("severity", "")
    if sev_raw:
        severity = sev_raw
    elif "total" in outage_type.lower():
        severity = "total"
    elif "major" in outage_type.lower() or "significant" in outage_type.lower():
        severity = "major"
    else:
        severity = "partial"

    return otype, scope, severity


def normalize_outage(annotation: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize a single Cloudflare Radar annotation into outage documents.

    One annotation may cover multiple locations/ASNs, so we may produce
    multiple documents.

    Returns:
        List of outage documents ready for indexing.
    """
    start_time = annotation.get("startDate") or annotation.get("eventDate", "")
    end_time = annotation.get("endDate") or None
    now = datetime.now(timezone.utc).isoformat()

    # Compute duration
    duration_hours: float | None = None
    if start_time and end_time:
        try:
            s = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            e = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
            duration_hours = round((e - s).total_seconds() / 3600, 2)
        except (ValueError, TypeError):
            pass

    # Status
    status = "resolved" if end_time else "ongoing"

    otype, scope, severity = classify_outage(annotation)

    locations = annotation.get("locations", [])
    asns = annotation.get("asns", [])

    docs: list[dict[str, Any]] = []

    if locations:
        for loc in locations:
            cc = loc if isinstance(loc, str) else loc.get("code", loc.get("name", ""))
            country = resolve_country(cc)
            outage_id = hashlib.sha256(
                f"{start_time}:{cc}:{annotation.get('id', '')}".encode()
            ).hexdigest()[:20]
            docs.append({
                "outage_id": outage_id,
                "date": start_time or now,
                "country": country,
                "country_code": cc.upper(),
                "asn": 0,
                "asn_name": "",
                "type": otype,
                "scope": scope,
                "duration_hours": duration_hours,
                "severity": severity,
                "status": status,
                "start_time": start_time or now,
                "end_time": end_time,
                "ingested_at": now,
            })
    elif asns:
        for asn_info in asns:
            asn_num = asn_info if isinstance(asn_info, int) else asn_info.get("asn", 0)
            asn_name = "" if isinstance(asn_info, int) else asn_info.get("name", "")
            cc = "" if isinstance(asn_info, int) else asn_info.get("country", "")
            country = resolve_country(cc) if cc else ""
            outage_id = hashlib.sha256(
                f"{start_time}:{asn_num}:{annotation.get('id', '')}".encode()
            ).hexdigest()[:20]
            docs.append({
                "outage_id": outage_id,
                "date": start_time or now,
                "country": country,
                "country_code": cc.upper() if cc else "",
                "asn": asn_num,
                "asn_name": asn_name,
                "type": "asn-level",
                "scope": "local",
                "duration_hours": duration_hours,
                "severity": severity,
                "status": status,
                "start_time": start_time or now,
                "end_time": end_time,
                "ingested_at": now,
            })
    else:
        # Minimal annotation with just an ID/date
        outage_id = hashlib.sha256(
            f"{start_time}:{annotation.get('id', '')}".encode()
        ).hexdigest()[:20]
        docs.append({
            "outage_id": outage_id,
            "date": start_time or now,
            "country": "",
            "country_code": "",
            "asn": 0,
            "asn_name": "",
            "type": otype,
            "scope": scope,
            "duration_hours": duration_hours,
            "severity": severity,
            "status": status,
            "start_time": start_time or now,
            "end_time": end_time,
            "ingested_at": now,
        })

    return docs
