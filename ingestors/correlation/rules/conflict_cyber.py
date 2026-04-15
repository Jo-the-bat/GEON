"""Correlation Rule 3: Armed conflict + cyber infrastructure activity.

Detects situations where active armed conflicts (ACLED: battles, violence
against civilians) coincide with cyber operations attributed to actors
from the same geographic zone in OpenCTI.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from elasticsearch import Elasticsearch
from pycti import OpenCTIApiClient

from common.config import INDEX_PREFIX
from common.opencti_client import get_campaigns_by_country

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
LOOKBACK_DAYS: int = 14
CYBER_WINDOW_DAYS: int = 30
ACLED_INDEX_PATTERN = f"{INDEX_PREFIX}-acled-events-*"

# ACLED event types that indicate active armed conflict.
CONFLICT_EVENT_TYPES = frozenset({
    "Battles",
    "Violence against civilians",
    "Explosions/Remote violence",
    "Strategic developments",
})


class ConflictCyberRule:
    """Rule 3: Armed conflict coinciding with cyber infrastructure activity.

    Queries Elasticsearch for ACLED events (battles, violence against
    civilians, etc.) and for each affected country or region, queries
    OpenCTI for cyber campaigns/intrusion sets from actors in the same
    zone.

    Attributes:
        es: Elasticsearch client.
        octi: OpenCTI API client.
        lookback_days: Number of days to scan for ACLED events.
    """

    RULE_NAME = "conflict_cyber_infrastructure"

    def __init__(
        self,
        es: Elasticsearch,
        octi: OpenCTIApiClient,
        lookback_days: int = LOOKBACK_DAYS,
    ) -> None:
        self.es = es
        self.octi = octi
        self.lookback_days = lookback_days

    def run(self) -> list[dict[str, Any]]:
        """Execute the rule and return a list of correlation documents.

        Returns:
            List of correlation dicts ready for indexing.
        """
        correlations: list[dict[str, Any]] = []

        # Step 1: Find recent armed-conflict events.
        conflict_countries = self._find_conflict_countries()
        if not conflict_countries:
            logger.info("[%s] No recent armed-conflict events found.", self.RULE_NAME)
            return correlations

        logger.info(
            "[%s] Found armed-conflict events in %d country/countries.",
            self.RULE_NAME,
            len(conflict_countries),
        )

        # Step 2: For each country, check for cyber activity.
        for country, conflict_events in conflict_countries.items():
            cyber_matches = self._find_cyber_activity(country)
            if not cyber_matches:
                continue

            correlation = self._build_correlation(
                country, conflict_events, cyber_matches
            )
            correlations.append(correlation)

        logger.info(
            "[%s] Generated %d correlation(s).", self.RULE_NAME, len(correlations)
        )
        return correlations

    # ------------------------------------------------------------------
    # Step 1: Find armed-conflict events
    # ------------------------------------------------------------------

    def _find_conflict_countries(self) -> dict[str, list[dict[str, Any]]]:
        """Query ES for ACLED events of conflict types in the lookback window.

        Returns:
            Dict mapping country names to lists of conflict event dicts.
        """
        since = (
            datetime.now(timezone.utc) - timedelta(days=self.lookback_days)
        ).isoformat()

        query: dict[str, Any] = {
            "bool": {
                "must": [
                    {"terms": {"event_type": list(CONFLICT_EVENT_TYPES)}},
                    {"range": {"event_date": {"gte": since}}},
                ],
            }
        }

        try:
            resp = self.es.search(
                index=ACLED_INDEX_PATTERN,
                query=query,
                size=1000,
                sort=[{"event_date": "desc"}],
            )
        except Exception:
            logger.exception("[%s] Failed to query ACLED index.", self.RULE_NAME)
            return {}

        hits = resp.get("hits", {}).get("hits", [])
        logger.debug("[%s] ACLED query returned %d hits.", self.RULE_NAME, len(hits))

        by_country: dict[str, list[dict[str, Any]]] = {}
        for hit in hits:
            src = hit["_source"]
            country = src.get("country", "").strip()
            if country:
                by_country.setdefault(country, []).append(src)

        return by_country

    # ------------------------------------------------------------------
    # Step 2: Find cyber activity from the same zone
    # ------------------------------------------------------------------

    def _find_cyber_activity(
        self, country: str
    ) -> list[dict[str, Any]]:
        """Query OpenCTI for campaigns/intrusion sets linked to a country.

        Args:
            country: Country name to search for.

        Returns:
            List of matching campaign/intrusion-set dicts.
        """
        campaigns = get_campaigns_by_country(
            self.octi, country, days_back=CYBER_WINDOW_DAYS
        )
        if campaigns:
            logger.info(
                "[%s] Found %d cyber campaign(s) for %s.",
                self.RULE_NAME,
                len(campaigns),
                country,
            )
        return campaigns

    # ------------------------------------------------------------------
    # Build correlation
    # ------------------------------------------------------------------

    def _build_correlation(
        self,
        country: str,
        conflict_events: list[dict[str, Any]],
        cyber_matches: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Assemble a correlation document.

        Args:
            country: Country where both conflict and cyber events occur.
            conflict_events: List of ACLED event dicts for this country.
            cyber_matches: List of APT campaigns/intrusion sets.

        Returns:
            Correlation document dict.
        """
        now = datetime.now(timezone.utc).isoformat()

        # Aggregate conflict statistics.
        total_fatalities = sum(e.get("fatalities", 0) for e in conflict_events)
        event_types = list({e.get("event_type", "") for e in conflict_events})
        num_events = len(conflict_events)

        # Determine severity.
        severity = self._compute_severity(num_events, total_fatalities, cyber_matches)

        # Primary APT match.
        primary_apt = cyber_matches[0]
        apt_name = primary_apt.get("name", "Unknown")
        apt_type = primary_apt.get("_geon_type", "campaign")

        # Build timeline.
        timeline: list[dict[str, str]] = []
        for evt in conflict_events[:5]:
            timeline.append({
                "date": evt.get("event_date", now),
                "type": "conflict",
                "description": (
                    f"{evt.get('event_type', 'Conflict')}: "
                    f"{evt.get('location', 'Unknown location')} "
                    f"({evt.get('fatalities', 0)} fatalities)"
                ),
            })
        for apt in cyber_matches[:3]:
            timeline.append({
                "date": apt.get("modified", apt.get("created", now)),
                "type": "cyber",
                "description": f"{apt.get('name', 'Unknown')} ({apt_type})",
            })

        # Sort timeline by date.
        timeline.sort(key=lambda t: t.get("date", ""))

        correlation_id = hashlib.sha256(
            f"{self.RULE_NAME}:{country}:{now[:10]}".encode()
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
                    f"{num_events} armed-conflict events in {country} "
                    f"(types: {', '.join(event_types)}; "
                    f"{total_fatalities} total fatalities)"
                ),
                "goldstein": 0.0,
            },
            "cyber_event": {
                "campaign_id": primary_apt.get("id", ""),
                "apt_group": apt_name,
                "techniques": [],
            },
            "description": (
                f"Active armed conflict in {country} ({num_events} events, "
                f"{total_fatalities} fatalities) coincides with cyber activity "
                f"by {apt_name} in the same zone."
            ),
            "timeline": timeline,
        }

    # ------------------------------------------------------------------
    # Severity computation
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_severity(
        num_events: int,
        total_fatalities: int,
        cyber_matches: list[dict[str, Any]],
    ) -> str:
        """Derive severity from conflict intensity and cyber activity.

        Args:
            num_events: Number of ACLED conflict events.
            total_fatalities: Total fatalities across conflict events.
            cyber_matches: List of matching cyber campaigns.

        Returns:
            Severity string.
        """
        score = 0

        # Conflict intensity.
        if num_events >= 50:
            score += 2
        elif num_events >= 10:
            score += 1

        if total_fatalities >= 100:
            score += 2
        elif total_fatalities >= 10:
            score += 1

        # Cyber intensity.
        if len(cyber_matches) >= 3:
            score += 1

        max_confidence = max(
            (m.get("confidence", 0) or 0 for m in cyber_matches),
            default=0,
        )
        if max_confidence >= 80:
            score += 1

        if score >= 5:
            return "critical"
        elif score >= 3:
            return "high"
        elif score >= 1:
            return "medium"
        return "low"
