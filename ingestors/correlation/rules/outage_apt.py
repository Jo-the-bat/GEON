"""Correlation Rule 9: Internet outage + APT activity.

Detects situations where a recent internet outage coincides with APT
activity — either offensive groups attributed to the country (suggesting
state-directed shutdown) or groups targeting the country (suggesting
attack-related disruption).
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from elasticsearch import Elasticsearch
from pycti import OpenCTIApiClient

from common.config import INDEX_PREFIX
from common.opencti_client import get_campaigns_by_country

logger = logging.getLogger(__name__)

OUTAGES_INDEX = f"{INDEX_PREFIX}-outages"
CTI_INDEX = f"{INDEX_PREFIX}-cti-threats"
APT_WINDOW_DAYS: int = 30

_APT_MAPPING_PATH = Path(__file__).resolve().parent.parent.parent / "common" / "country_apt_mapping.json"
_COUNTRY_APT_MAP: dict[str, list[str]] = {}
try:
    with _APT_MAPPING_PATH.open() as f:
        _raw = json.load(f)
    _COUNTRY_APT_MAP = {k: v for k, v in _raw.items() if k != "_comment"}
except Exception:
    pass


class OutageAPTRule:
    """Rule 9: Internet outage coinciding with APT activity."""

    RULE_NAME = "outage_apt_activity"

    def __init__(self, es: Elasticsearch, octi: OpenCTIApiClient | None = None) -> None:
        self.es = es
        self.octi = octi

    def run(self) -> list[dict[str, Any]]:
        correlations: list[dict[str, Any]] = []

        outages = self._find_recent_outages()
        if not outages:
            logger.info("[%s] No recent outages found.", self.RULE_NAME)
            return correlations

        logger.info("[%s] Checking %d outage(s) against APT data.", self.RULE_NAME, len(outages))

        for outage in outages:
            country = outage.get("country", "")
            if not country:
                continue

            # a) APT groups attributed TO the country (offensive — state shutdown?)
            offensive_apts = self._find_offensive_apts(country)

            # b) APT groups targeting the country (defensive — attack-related?)
            targeting_apts = self._find_targeting_apts(country)

            if not offensive_apts and not targeting_apts:
                continue

            correlation = self._build_correlation(outage, offensive_apts, targeting_apts)
            correlations.append(correlation)

        logger.info("[%s] Generated %d correlation(s).", self.RULE_NAME, len(correlations))
        return correlations

    def _find_recent_outages(self) -> list[dict[str, Any]]:
        """Find outages from the last 48 hours."""
        try:
            resp = self.es.search(
                index=OUTAGES_INDEX,
                query={
                    "bool": {
                        "filter": [
                            {"range": {"start_time": {"gte": "now-48h"}}},
                        ],
                        "must_not": [{"term": {"country": ""}}],
                    }
                },
                size=50,
                sort=[{"start_time": "desc"}],
            )
            return [h["_source"] for h in resp["hits"]["hits"]]
        except Exception:
            logger.exception("[%s] Failed to query outages.", self.RULE_NAME)
            return []

    def _find_offensive_apts(self, country: str) -> list[dict[str, Any]]:
        """Find APT groups attributed to this country with recent activity."""
        known_apts = _COUNTRY_APT_MAP.get(country, [])
        if not known_apts:
            return []

        # Check OpenCTI for recent campaigns
        if self.octi:
            try:
                campaigns = get_campaigns_by_country(self.octi, country, days_back=APT_WINDOW_DAYS)
                if campaigns:
                    return [{"name": c.get("name", ""), "type": "offensive",
                             "id": c.get("id", ""), "source": "opencti"} for c in campaigns]
            except Exception:
                pass

        # Check ES CTI index for recent threats
        try:
            should_clauses = [{"match": {"name": apt}} for apt in known_apts[:10]]
            resp = self.es.search(
                index=CTI_INDEX,
                query={
                    "bool": {
                        "should": should_clauses,
                        "minimum_should_match": 1,
                    }
                },
                size=5,
            )
            if resp["hits"]["hits"]:
                return [{"name": h["_source"].get("name", ""), "type": "offensive",
                         "id": h["_id"], "source": "es_cti"} for h in resp["hits"]["hits"]]
        except Exception:
            pass

        # Fallback: return static attribution
        return [{"name": apt, "type": "offensive", "id": "", "source": "static"} for apt in known_apts[:3]]

    def _find_targeting_apts(self, country: str) -> list[dict[str, Any]]:
        """Find APT groups known to target this country."""
        # Search CTI index for threats mentioning this country as target
        try:
            resp = self.es.search(
                index=CTI_INDEX,
                query={
                    "bool": {
                        "filter": [
                            {"bool": {"should": [
                                {"term": {"target_countries": country}},
                                {"match": {"description": country}},
                            ], "minimum_should_match": 1}},
                        ]
                    }
                },
                size=5,
            )
            return [{"name": h["_source"].get("name", ""), "type": "targeting",
                     "id": h["_id"], "source": "es_cti"} for h in resp["hits"]["hits"]]
        except Exception:
            return []

    def _build_correlation(
        self,
        outage: dict[str, Any],
        offensive_apts: list[dict[str, Any]],
        targeting_apts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        country = outage["country"]
        is_national = outage.get("type") == "country-level" or outage.get("scope") == "national"

        # Critical if national outage + offensive APT from same country (state shutdown)
        if is_national and offensive_apts:
            severity = "critical"
        elif offensive_apts or targeting_apts:
            severity = "high"
        else:
            severity = "medium"

        all_apts = offensive_apts + targeting_apts
        apt_names = [a["name"] for a in all_apts if a.get("name")]
        primary_apt = all_apts[0] if all_apts else {}

        correlation_id = hashlib.sha256(
            f"{self.RULE_NAME}:{country}:{outage.get('start_time', '')}".encode()
        ).hexdigest()[:20]

        desc_parts = [f"Internet outage in {country} ({outage.get('severity', 'unknown')}, {outage.get('type', '')})"]
        if offensive_apts:
            desc_parts.append(f"coincides with APT groups attributed to {country}: {', '.join(a['name'] for a in offensive_apts[:3])}")
        if targeting_apts:
            desc_parts.append(f"APT groups targeting {country}: {', '.join(a['name'] for a in targeting_apts[:3])}")

        return {
            "correlation_id": correlation_id,
            "timestamp": now,
            "date": now,
            "rule_name": self.RULE_NAME,
            "severity": severity,
            "countries_involved": [country],
            "diplomatic_event": {
                "event_id": outage.get("outage_id", ""),
                "description": f"Internet outage: {outage.get('severity', '')} / {outage.get('duration_hours', 'unknown')}h",
                "goldstein": 0.0,
            },
            "cyber_event": {
                "campaign_id": primary_apt.get("id", ""),
                "apt_group": primary_apt.get("name", ""),
                "techniques": [],
            },
            "description": ". ".join(desc_parts) + ".",
            "timeline": [
                {"date": outage.get("start_time", now), "type": "internet_outage",
                 "description": f"Outage in {country}: {outage.get('severity', '')} / {outage.get('type', '')}"},
            ] + [
                {"date": now, "type": "apt_activity",
                 "description": f"APT: {a['name']} ({a['type']})"} for a in all_apts[:3]
            ],
        }
