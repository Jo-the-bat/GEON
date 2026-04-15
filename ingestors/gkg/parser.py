"""GEON GDELT GKG response parser.

Parses the GDELT v2 Global Knowledge Graph (GKG) CSV into structured
documents for Elasticsearch.  The GKG enriches GDELT Events with themes,
entities (persons, organizations), tone analysis, and GCAM scores.

GKG CSV columns (tab-separated, no header):
  0  GKGRECORDID
  1  DATE (YYYYMMDDHHmmSS)
  2  SourceCollectionIdentifier
  3  SourceCommonName
  4  DocumentIdentifier (URL)
  5  Counts
  6  V2Counts
  7  Themes
  8  V2Themes
  9  Locations
 10  V2Locations
 11  Persons
 12  V2Persons
 13  Organizations
 14  V2Organizations
 15  V2Tone (6 comma-separated floats)
 16  Dates
 17  GCAM
 18  SharingImage
 19  RelatedImages
 20  SocialImageEmbeds
 21  SocialVideoEmbeds
 22  Quotations
 23  AllNames
 24  Amounts
 25  TranslationInfo
 26  Extras
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

GKG_COLUMN_COUNT = 27


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _split_semicolons(raw: str) -> list[str]:
    """Split a semicolon-delimited string, stripping empty entries."""
    if not raw:
        return []
    return [s.strip() for s in raw.split(";") if s.strip()]


def _parse_v2_locations(raw: str) -> list[dict[str, Any]]:
    """Parse V2Locations field into structured location dicts.

    V2Locations format: ``type#name#countrycode#adm1#lat#long#featureid;...``
    """
    if not raw:
        return []
    locations: list[dict[str, Any]] = []
    for entry in raw.split(";"):
        parts = entry.split("#")
        if len(parts) < 6:
            continue
        lat = _safe_float(parts[4])
        lon = _safe_float(parts[5])
        if lat == 0.0 and lon == 0.0:
            continue
        locations.append({
            "name": parts[1].strip(),
            "country_code": parts[2].strip(),
            "lat": lat,
            "lon": lon,
        })
    return locations


def _parse_tone(raw: str) -> dict[str, float | int]:
    """Parse V2Tone CSV into individual tone components.

    Format: ``tone,pos_score,neg_score,polarity,activity_density,self_group_density,word_count``
    """
    parts = raw.split(",") if raw else []
    return {
        "tone": _safe_float(parts[0]) if len(parts) > 0 else 0.0,
        "tone_positive": _safe_float(parts[1]) if len(parts) > 1 else 0.0,
        "tone_negative": _safe_float(parts[2]) if len(parts) > 2 else 0.0,
        "tone_polarity": _safe_float(parts[3]) if len(parts) > 3 else 0.0,
        "tone_activity_density": _safe_float(parts[4]) if len(parts) > 4 else 0.0,
        "tone_self_group_density": _safe_float(parts[5]) if len(parts) > 5 else 0.0,
        "tone_word_count": _safe_int(parts[6]) if len(parts) > 6 else 0,
    }


def _parse_gcam(raw: str) -> dict[str, float]:
    """Parse GCAM scores into a flat dict.

    Format: ``dimension.code:value,dimension.code:value,...``
    Only keeps the first 50 scores to avoid document bloat.
    """
    if not raw:
        return {}
    scores: dict[str, float] = {}
    for pair in raw.split(",")[:50]:
        kv = pair.split(":")
        if len(kv) == 2:
            scores[kv[0].strip()] = _safe_float(kv[1])
    return scores


def parse_gkg_csv(csv_text: str) -> list[dict[str, Any]]:
    """Parse a GDELT v2 GKG CSV into a list of document dicts.

    Args:
        csv_text: Raw tab-delimited GKG CSV content.

    Returns:
        List of normalized dicts ready for ES indexing.
    """
    docs: list[dict[str, Any]] = []
    reader = csv.reader(io.StringIO(csv_text), delimiter="\t")
    for row in reader:
        if len(row) < GKG_COLUMN_COUNT:
            continue

        gkg_id = row[0].strip()
        date_raw = row[1].strip()
        source_name = row[3].strip()
        source_url = row[4].strip()
        themes_raw = row[8].strip() if row[8] else row[7].strip()
        locations_raw = row[10].strip() if row[10] else row[9].strip()
        persons_raw = row[12].strip() if row[12] else row[11].strip()
        orgs_raw = row[14].strip() if row[14] else row[13].strip()
        tone_raw = row[15].strip()
        gcam_raw = row[17].strip()

        # Parse date
        try:
            if len(date_raw) == 14:
                event_date = datetime.strptime(date_raw, "%Y%m%d%H%M%S").replace(
                    tzinfo=timezone.utc,
                )
            else:
                event_date = datetime.now(tz=timezone.utc)
        except (ValueError, TypeError):
            event_date = datetime.now(tz=timezone.utc)

        # Parse components
        themes = _split_semicolons(themes_raw)
        persons = _split_semicolons(persons_raw)
        organizations = _split_semicolons(orgs_raw)
        locations = _parse_v2_locations(locations_raw)
        tone_data = _parse_tone(tone_raw)
        gcam = _parse_gcam(gcam_raw)

        if not gkg_id:
            gkg_id = hashlib.sha256(
                f"{date_raw}{source_url}".encode()
            ).hexdigest()[:20]

        now = datetime.now(tz=timezone.utc).isoformat()
        doc: dict[str, Any] = {
            "gkg_id": gkg_id,
            "date": event_date.isoformat(),
            "source_url": source_url,
            "source_name": source_name,
            "themes": themes[:100],
            "persons": persons[:50],
            "organizations": organizations[:50],
            "locations": locations[:20],
            "num_articles": 1,
            "gcam_scores": gcam,
            "ingested_at": now,
            **tone_data,
        }
        docs.append(doc)

    logger.info("GKG CSV: parsed %d rows.", len(docs))
    return docs
