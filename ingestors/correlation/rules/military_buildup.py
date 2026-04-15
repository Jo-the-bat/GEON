"""Correlation Rule 6: Military spending increase + APT activity.

Detects countries with significant military spending increases (>10% YoY)
that also have attributed APT groups active in OpenCTI. This is a slow
correlation (annual data) but strategically relevant.
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

SPENDING_INDEX = f"{INDEX_PREFIX}-military-spending"
YOY_THRESHOLD = 10.0  # Percent

_APT_MAPPING_PATH = Path(__file__).resolve().parent.parent.parent / "common" / "country_apt_mapping.json"
_COUNTRY_APT_MAP: dict[str, list[str]] = {}
try:
    with _APT_MAPPING_PATH.open() as f:
        _raw = json.load(f)
    _COUNTRY_APT_MAP = {k: v for k, v in _raw.items() if k != "_comment"}
except Exception:
    pass


class MilitaryBuildupRule:
    """Rule 6: Military spending surge + APT activity."""

    RULE_NAME = "military_buildup_cyber"

    def __init__(self, es: Elasticsearch, octi: OpenCTIApiClient) -> None:
        self.es = es
        self.octi = octi

    def run(self) -> list[dict[str, Any]]:
        correlations: list[dict[str, Any]] = []

        high_spenders = self._find_high_increase_countries()
        if not high_spenders:
            logger.info("[%s] No countries with >%.0f%% spending increase.", self.RULE_NAME, YOY_THRESHOLD)
            return correlations

        logger.info("[%s] Found %d countries with high spending increases.", self.RULE_NAME, len(high_spenders))

        for spending in high_spenders:
            country = spending["country"]
            apt_matches = self._find_apt_activity(country)
            if not apt_matches:
                continue

            correlation = self._build_correlation(spending, apt_matches)
            correlations.append(correlation)

        logger.info("[%s] Generated %d correlation(s).", self.RULE_NAME, len(correlations))
        return correlations

    def _find_high_increase_countries(self) -> list[dict[str, Any]]:
        """Find countries with YoY spending increase > threshold."""
        try:
            resp = self.es.search(
                index=SPENDING_INDEX,
                query={
                    "bool": {
                        "filter": [
                            {"range": {"spending_change_yoy_pct": {"gt": YOY_THRESHOLD}}},
                        ]
                    }
                },
                size=50,
                sort=[{"spending_change_yoy_pct": "desc"}],
            )
            return [h["_source"] for h in resp["hits"]["hits"]]
        except Exception:
            logger.exception("[%s] Failed to query spending data.", self.RULE_NAME)
            return []

    def _find_apt_activity(self, country: str) -> list[dict[str, Any]]:
        """Check for APT activity attributed to this country."""
        # First check static mapping
        known_apts = _COUNTRY_APT_MAP.get(country, [])
        if not known_apts:
            return []

        # Then check OpenCTI for active campaigns
        if self.octi:
            try:
                campaigns = get_campaigns_by_country(self.octi, country, days_back=365)
                if campaigns:
                    return campaigns
            except Exception:
                pass

        # Return static APT info as fallback
        return [{"name": apt, "_geon_type": "intrusion-set"} for apt in known_apts]

    def _build_correlation(
        self,
        spending: dict[str, Any],
        apt_matches: list[dict[str, Any]],
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        country = spending["country"]
        yoy = spending.get("spending_change_yoy_pct", 0)
        primary_apt = apt_matches[0] if apt_matches else {}
        apt_name = primary_apt.get("name", "Unknown APT")

        severity = "high" if yoy > 20 else "medium"

        correlation_id = hashlib.sha256(
            f"{self.RULE_NAME}:{country}:{spending.get('year', '')}".encode()
        ).hexdigest()[:20]

        return {
            "correlation_id": correlation_id,
            "timestamp": now,
            "date": now,
            "rule_name": self.RULE_NAME,
            "severity": severity,
            "countries_involved": [country],
            "diplomatic_event": {
                "event_id": "",
                "description": (
                    f"Military spending increase of {yoy:.1f}% YoY "
                    f"(${spending.get('spending_usd_millions', 0):.0f}M USD)"
                ),
                "goldstein": 0.0,
            },
            "cyber_event": {
                "campaign_id": primary_apt.get("id", ""),
                "apt_group": apt_name,
                "techniques": [],
            },
            "description": (
                f"{country} increased military spending by {yoy:.1f}% YoY while "
                f"APT group {apt_name} attributed to {country} remains active. "
                f"Strategic correlation: military buildup coincides with cyber operations."
            ),
            "timeline": [
                {
                    "date": f"{spending.get('year', 2024)}-01-01T00:00:00Z",
                    "type": "military_spending",
                    "description": f"Spending increase: {yoy:.1f}% YoY",
                },
            ],
        }
