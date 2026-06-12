"""Parser for Cloudflare Radar outage annotations.

Normalizes raw API responses into the ``geon-outages`` document schema.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

from common.countries import normalize_country

logger = logging.getLogger(__name__)


def resolve_country(code: str) -> str:
    """Resolve a 2-letter country code to the GEON canonical country name.

    Delegates to the shared canonical dimension (the previous local map
    diverged from GDELT naming on Cote d'Ivoire and the two Congos, and
    was missing ~20 countries that surfaced as bare ISO2 codes).
    """
    return normalize_country(code)


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
