"""Correlation Rule 5: Internet outage + diplomatic/military escalation.

Detects situations where a national or major internet outage coincides
with GDELT diplomatic escalation (Goldstein < -5) or ACLED armed
conflict in the same country within a +/-48 hour window.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from elasticsearch import Elasticsearch

from common.config import INDEX_PREFIX

logger = logging.getLogger(__name__)

WINDOW_HOURS: int = 48
GOLDSTEIN_THRESHOLD: float = -5.0
OUTAGES_INDEX = f"{INDEX_PREFIX}-outages"
GDELT_INDEX_PATTERN = f"{INDEX_PREFIX}-gdelt-events-*"
ACLED_INDEX_PATTERN = f"{INDEX_PREFIX}-acled-*"


class InternetOutageRule:
    """Rule 5: Internet outage + escalation/conflict.

    Queries for recent national/major outages, then looks for GDELT
    diplomatic escalations or ACLED conflict events in the same country
    within +/-48 hours.
    """

    RULE_NAME = "internet_outage_escalation"

    def __init__(self, es: Elasticsearch, octi: Any = None) -> None:
        self.es = es

    def run(self) -> list[dict[str, Any]]:
        correlations: list[dict[str, Any]] = []

        outages = self._find_recent_outages()
        if not outages:
            logger.info("[%s] No qualifying outages found.", self.RULE_NAME)
            return correlations

        logger.info("[%s] Found %d outage(s) to check.", self.RULE_NAME, len(outages))

        for outage in outages:
            country = outage.get("country", "")
            if not country:
                continue

            start_time = outage.get("start_time", "")
            gdelt_hits = self._find_gdelt_escalation(country, start_time)
            acled_hits = self._find_acled_conflict(country, start_time)

            if not gdelt_hits and not acled_hits:
                continue

            correlation = self._build_correlation(outage, gdelt_hits, acled_hits)
            correlations.append(correlation)

        logger.info("[%s] Generated %d correlation(s).", self.RULE_NAME, len(correlations))
        return correlations

    def _find_recent_outages(self) -> list[dict[str, Any]]:
        """Find outages that are national/major from the last 7 days."""
        try:
            resp = self.es.search(
                index=OUTAGES_INDEX,
                query={
                    "bool": {
                        "filter": [
                            {"range": {"start_time": {"gte": "now-7d"}}},
                            {"bool": {"should": [
                                {"term": {"type": "country-level"}},
                                {"term": {"scope": "national"}},
                                {"terms": {"severity": ["major", "total"]}},
                            ], "minimum_should_match": 1}},
                        ],
                        "must_not": [{"term": {"country": ""}}],
                    }
                },
                size=100,
                sort=[{"start_time": "desc"}],
            )
            return [h["_source"] for h in resp["hits"]["hits"]]
        except Exception:
            logger.exception("[%s] Failed to query outages.", self.RULE_NAME)
            return []

    def _find_gdelt_escalation(
        self, country: str, ref_time: str
    ) -> list[dict[str, Any]]:
        """Find GDELT events with Goldstein < threshold near the outage."""
        window_start, window_end = self._time_window(ref_time)
        try:
            resp = self.es.search(
                index=GDELT_INDEX_PATTERN,
                query={
                    "bool": {
                        "filter": [
                            {"range": {"date": {"gte": window_start, "lte": window_end}}},
                            {"range": {"goldstein_scale": {"lt": GOLDSTEIN_THRESHOLD}}},
                            {"bool": {"should": [
                                {"term": {"source_country": country}},
                                {"term": {"target_country": country}},
                            ], "minimum_should_match": 1}},
                        ]
                    }
                },
                size=10,
                sort=[{"goldstein_scale": "asc"}],
            )
            return [h["_source"] for h in resp["hits"]["hits"]]
        except Exception:
            return []

    def _find_acled_conflict(
        self, country: str, ref_time: str
    ) -> list[dict[str, Any]]:
        """Find ACLED conflict events near the outage."""
        window_start, window_end = self._time_window(ref_time)
        try:
            resp = self.es.search(
                index=ACLED_INDEX_PATTERN,
                query={
                    "bool": {
                        "filter": [
                            {"range": {"date": {"gte": window_start, "lte": window_end}}},
                            {"term": {"country": country}},
                        ]
                    }
                },
                size=10,
            )
            return [h["_source"] for h in resp["hits"]["hits"]]
        except Exception:
            return []

    def _build_correlation(
        self,
        outage: dict[str, Any],
        gdelt_hits: list[dict[str, Any]],
        acled_hits: list[dict[str, Any]],
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        country = outage["country"]

        is_total = outage.get("severity") == "total"
        has_conflict = bool(acled_hits)
        has_diplomatic = bool(gdelt_hits)

        if is_total and has_conflict:
            severity = "critical"
        elif is_total or has_conflict:
            severity = "high"
        elif has_diplomatic:
            severity = "high"
        else:
            severity = "medium"

        worst_goldstein = None
        worst_event_desc = ""
        if gdelt_hits:
            worst = min(gdelt_hits, key=lambda e: e.get("goldstein_scale", 0))
            worst_goldstein = worst.get("goldstein_scale")
            worst_event_desc = worst.get("cameo_description", "")

        timeline: list[dict[str, str]] = [
            {
                "date": outage.get("start_time", now),
                "type": "internet_outage",
                "description": (
                    f"{outage.get('severity', 'unknown')} internet outage in {country} "
                    f"({outage.get('type', '')})"
                ),
            }
        ]
        for evt in gdelt_hits[:3]:
            timeline.append({
                "date": evt.get("date", now),
                "type": "diplomatic",
                "description": f"Goldstein {evt.get('goldstein_scale')}: {evt.get('cameo_description', '')}",
            })
        for evt in acled_hits[:3]:
            timeline.append({
                "date": evt.get("date", now),
                "type": "conflict",
                "description": f"ACLED: {evt.get('event_type', '')} — {evt.get('notes', '')[:100]}",
            })

        correlation_id = hashlib.sha256(
            f"{self.RULE_NAME}:{country}:{outage.get('start_time', '')}".encode()
        ).hexdigest()[:20]

        desc_parts = [
            f"Internet outage ({outage.get('severity', 'unknown')}) in {country}"
        ]
        if has_diplomatic:
            desc_parts.append(
                f"coinciding with diplomatic escalation (Goldstein {worst_goldstein})"
            )
        if has_conflict:
            desc_parts.append(f"and {len(acled_hits)} ACLED conflict event(s)")

        return {
            "correlation_id": correlation_id,
            "timestamp": now,
            "date": now,
            "rule_name": self.RULE_NAME,
            "severity": severity,
            "countries_involved": [country],
            "diplomatic_event": {
                "event_id": gdelt_hits[0].get("event_id", "") if gdelt_hits else "",
                "description": worst_event_desc,
                "goldstein": worst_goldstein or 0.0,
            },
            "cyber_event": {
                "campaign_id": outage.get("outage_id", ""),
                "apt_group": "",
                "techniques": [],
            },
            "description": " ".join(desc_parts) + ".",
            "timeline": timeline,
        }

    @staticmethod
    def _time_window(ref_time: str) -> tuple[str, str]:
        try:
            dt = datetime.fromisoformat(ref_time.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            dt = datetime.now(timezone.utc)
        start = (dt - timedelta(hours=WINDOW_HOURS)).isoformat()
        end = (dt + timedelta(hours=WINDOW_HOURS)).isoformat()
        return start, end
