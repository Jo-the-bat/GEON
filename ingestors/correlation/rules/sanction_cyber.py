"""Correlation Rule 2: Sanctions + cyber activity spike.

Detects situations where new sanctions against a country or entity are
followed by a significant increase (> 200%) in cyber indicators (IoCs)
linked to that country within a 60-day window.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from elasticsearch import Elasticsearch
from pycti import OpenCTIApiClient

from common.config import INDEX_PREFIX
from common.opencti_client import get_indicators_by_country

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
LOOKBACK_DAYS: int = 14
IOC_WINDOW_DAYS: int = 60
IOC_SPIKE_THRESHOLD: float = 2.0  # 200% increase
SANCTIONS_INDEX = f"{INDEX_PREFIX}-sanctions"
CTI_INDICATORS_PATTERN = f"{INDEX_PREFIX}-cti-indicators"


class SanctionCyberRule:
    """Rule 2: New sanctions followed by a cyber indicator spike.

    Queries Elasticsearch for recently indexed sanctions, then compares
    IoC volume for each sanctioned country in the 60 days after the
    sanction vs. the 60 days before.

    Attributes:
        es: Elasticsearch client.
        octi: OpenCTI API client.
        lookback_days: Number of days to look back for new sanctions.
    """

    RULE_NAME = "sanction_cyber_spike"

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

        # Step 1: Find recently ingested sanctions.
        sanctions = self._find_recent_sanctions()
        if not sanctions:
            logger.info("[%s] No recent sanctions found.", self.RULE_NAME)
            return correlations

        # Step 2: Group sanctions by country.
        countries_sanctioned = self._group_by_country(sanctions)
        logger.info(
            "[%s] Found sanctions for %d country/countries.",
            self.RULE_NAME,
            len(countries_sanctioned),
        )

        # Step 3: For each country, check for IoC spike.
        for country, sanction_docs in countries_sanctioned.items():
            spike_ratio = self._compute_ioc_spike(country)
            if spike_ratio is None:
                continue

            if spike_ratio >= IOC_SPIKE_THRESHOLD:
                correlation = self._build_correlation(
                    country, sanction_docs, spike_ratio
                )
                correlations.append(correlation)
                logger.info(
                    "[%s] IoC spike detected for %s: %.1f%% increase.",
                    self.RULE_NAME,
                    country,
                    spike_ratio * 100,
                )

        logger.info(
            "[%s] Generated %d correlation(s).", self.RULE_NAME, len(correlations)
        )
        return correlations

    # ------------------------------------------------------------------
    # Step 1: Recent sanctions
    # ------------------------------------------------------------------

    def _find_recent_sanctions(self) -> list[dict[str, Any]]:
        """Query ES for sanctions ingested in the last N days.

        Returns:
            List of sanctions document dicts.
        """
        since = (
            datetime.now(timezone.utc) - timedelta(days=self.lookback_days)
        ).isoformat()

        try:
            resp = self.es.search(
                index=SANCTIONS_INDEX,
                query={
                    "range": {"ingested_at": {"gte": since}},
                },
                size=500,
            )
        except Exception:
            logger.exception("[%s] Failed to query sanctions index.", self.RULE_NAME)
            return []

        hits = resp.get("hits", {}).get("hits", [])
        return [h["_source"] for h in hits]

    # ------------------------------------------------------------------
    # Step 2: Group by country
    # ------------------------------------------------------------------

    @staticmethod
    def _group_by_country(
        sanctions: list[dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        """Group sanctions by their country field.

        Args:
            sanctions: List of sanctions documents.

        Returns:
            Dict mapping country names to lists of sanctions docs.
        """
        by_country: dict[str, list[dict[str, Any]]] = {}
        for doc in sanctions:
            country = doc.get("country", "").strip()
            if country:
                by_country.setdefault(country, []).append(doc)
        return by_country

    # ------------------------------------------------------------------
    # Step 3: IoC spike computation
    # ------------------------------------------------------------------

    def _compute_ioc_spike(self, country: str) -> float | None:
        """Compare IoC volume before and after sanctions for a country.

        Looks at two 60-day windows:
        - **Baseline**: from 120 days ago to 60 days ago.
        - **Post-sanction**: last 60 days.

        Args:
            country: Country name to check.

        Returns:
            Ratio of post/baseline IoC counts, or ``None`` if the
            baseline is empty (no comparison possible).
        """
        now = datetime.now(timezone.utc)
        post_start = now - timedelta(days=IOC_WINDOW_DAYS)
        baseline_start = now - timedelta(days=IOC_WINDOW_DAYS * 2)
        baseline_end = now - timedelta(days=IOC_WINDOW_DAYS)

        # --- Count indicators in the post-sanction window ---
        post_count = self._count_iocs_in_window(
            country, post_start.isoformat(), now.isoformat()
        )

        # --- Count indicators in the baseline window ---
        baseline_count = self._count_iocs_in_window(
            country, baseline_start.isoformat(), baseline_end.isoformat()
        )

        if baseline_count == 0:
            if post_count > 0:
                logger.info(
                    "[%s] Country %s: %d IoCs in post-window, 0 in baseline "
                    "(treating as spike).",
                    self.RULE_NAME,
                    country,
                    post_count,
                )
                # Treat any activity against a zero baseline as a maximum spike.
                return IOC_SPIKE_THRESHOLD + 1.0
            return None

        ratio = post_count / baseline_count
        logger.debug(
            "[%s] Country %s: baseline=%d, post=%d, ratio=%.2f",
            self.RULE_NAME,
            country,
            baseline_count,
            post_count,
            ratio,
        )
        return ratio

    def _count_iocs_in_window(
        self, country: str, since: str, until: str
    ) -> int:
        """Count IoCs for a country in a given time window.

        First checks the CTI indicators index in Elasticsearch, then
        augments with OpenCTI if the ES index is empty.

        Args:
            country: Country name.
            since: Start of window (ISO format).
            until: End of window (ISO format).

        Returns:
            Number of IoCs found.
        """
        # Try Elasticsearch first.
        es_count = self._count_iocs_es(country, since, until)
        if es_count > 0:
            return es_count

        # Fall back to OpenCTI.
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            until_dt = datetime.fromisoformat(until.replace("Z", "+00:00"))
            days_back = (until_dt - since_dt).days
            indicators = get_indicators_by_country(
                self.octi, country, days_back=max(days_back, 1)
            )
            return len(indicators)
        except Exception:
            logger.exception(
                "[%s] Failed to query OpenCTI for IoCs (country=%s).",
                self.RULE_NAME,
                country,
            )
            return 0

    def _count_iocs_es(self, country: str, since: str, until: str) -> int:
        """Count IoCs in the CTI indicators ES index.

        Args:
            country: Country name.
            since: Window start.
            until: Window end.

        Returns:
            Count of matching documents.
        """
        try:
            resp = self.es.count(
                index=CTI_INDICATORS_PATTERN,
                query={
                    "bool": {
                        "must": [
                            {"term": {"country": country}},
                            {"range": {"ingested_at": {"gte": since, "lte": until}}},
                        ],
                    }
                },
            )
            return resp.get("count", 0)
        except Exception:
            logger.debug(
                "[%s] CTI indicators index not available for ES count.",
                self.RULE_NAME,
            )
            return 0

    # ------------------------------------------------------------------
    # Build correlation
    # ------------------------------------------------------------------

    def _build_correlation(
        self,
        country: str,
        sanction_docs: list[dict[str, Any]],
        spike_ratio: float,
    ) -> dict[str, Any]:
        """Assemble a correlation document.

        Args:
            country: Sanctioned country.
            sanction_docs: List of relevant sanctions documents.
            spike_ratio: IoC increase ratio.

        Returns:
            Correlation document dict.
        """
        now = datetime.now(timezone.utc).isoformat()

        # Severity based on spike magnitude.
        if spike_ratio >= 5.0:
            severity = "critical"
        elif spike_ratio >= 3.0:
            severity = "high"
        elif spike_ratio >= IOC_SPIKE_THRESHOLD:
            severity = "medium"
        else:
            severity = "low"

        programs = []
        for s in sanction_docs[:5]:
            programs.extend(s.get("programs", []))
        programs = list(set(programs))

        correlation_id = hashlib.sha256(
            f"{self.RULE_NAME}:{country}:{now[:10]}".encode()
        ).hexdigest()[:20]

        # Timeline.
        timeline: list[dict[str, str]] = []
        for s in sanction_docs[:5]:
            timeline.append({
                "date": s.get("ingested_at", now),
                "type": "sanction",
                "description": (
                    f"Sanction: {s.get('name', 'Unknown')} "
                    f"({s.get('sanctions_source', 'Unknown')})"
                ),
            })
        timeline.append({
            "date": now,
            "type": "cyber",
            "description": f"IoC spike: {spike_ratio:.0%} increase for {country}",
        })

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
                    f"Sanctions against {country} "
                    f"(programs: {', '.join(programs) or 'N/A'})"
                ),
                "goldstein": 0.0,
            },
            "cyber_event": {
                "campaign_id": "",
                "apt_group": "",
                "techniques": [],
            },
            "description": (
                f"Sanctions against {country} followed by a {spike_ratio:.0%} "
                f"increase in cyber indicators within {IOC_WINDOW_DAYS} days. "
                f"Programs: {', '.join(programs) or 'N/A'}."
            ),
            "timeline": timeline,
        }
