"""Correlation Rule 8: Prediction market movement + real-world event.

Detects when a significant Polymarket price movement (>10% in 72h)
coincides with a high-severity GDELT event (|Goldstein| > 7) involving
the same countries — measuring whether markets anticipate or react to
crises.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from elasticsearch import Elasticsearch

from common.config import INDEX_PREFIX

logger = logging.getLogger(__name__)

POLYMARKET_INDEX = f"{INDEX_PREFIX}-polymarket-cases"
GDELT_INDEX_PATTERN = f"{INDEX_PREFIX}-gdelt-events-*"
PRICE_SHIFT_THRESHOLD: float = 0.10  # 10%
GOLDSTEIN_SEVERITY: float = 7.0
WINDOW_HOURS: int = 72


class PredictionValidatedRule:
    """Rule 8: Prediction market price shift validated by real event."""

    RULE_NAME = "prediction_event_match"

    def __init__(self, es: Elasticsearch, octi: Any = None) -> None:
        self.es = es

    def run(self) -> list[dict[str, Any]]:
        correlations: list[dict[str, Any]] = []

        movers = self._find_significant_movers()
        if not movers:
            logger.info("[%s] No significant Polymarket movers found.", self.RULE_NAME)
            return correlations

        logger.info("[%s] Checking %d market(s) with significant movement.", self.RULE_NAME, len(movers))

        for case in movers:
            countries = case.get("countries_involved", [])
            if not countries:
                continue

            events = self._find_high_severity_events(countries)
            if not events:
                continue

            correlation = self._build_correlation(case, events)
            correlations.append(correlation)

        logger.info("[%s] Generated %d correlation(s).", self.RULE_NAME, len(correlations))
        return correlations

    def _find_significant_movers(self) -> list[dict[str, Any]]:
        """Find Polymarket cases with >10% price change in recent window."""
        try:
            # Look for cases with significant 24h or 7d price changes
            resp = self.es.search(
                index=POLYMARKET_INDEX,
                query={
                    "bool": {
                        "filter": [
                            {"term": {"status": "active"}},
                            {"range": {"date": {"gte": "now-7d"}}},
                        ],
                        "should": [
                            {"range": {"price_change_24h": {"gt": PRICE_SHIFT_THRESHOLD}}},
                            {"range": {"price_change_24h": {"lt": -PRICE_SHIFT_THRESHOLD}}},
                            {"range": {"price_change_7d": {"gt": PRICE_SHIFT_THRESHOLD}}},
                            {"range": {"price_change_7d": {"lt": -PRICE_SHIFT_THRESHOLD}}},
                        ],
                        "minimum_should_match": 1,
                    }
                },
                size=50,
                sort=[{"date": "desc"}],
            )
            return [h["_source"] for h in resp["hits"]["hits"]]
        except Exception:
            logger.exception("[%s] Failed to query Polymarket.", self.RULE_NAME)
            return []

    def _find_high_severity_events(self, countries: list[str]) -> list[dict[str, Any]]:
        """Find GDELT events with |Goldstein| > threshold for given countries in 72h."""
        since = (datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)).isoformat()

        country_clauses = []
        for c in countries:
            country_clauses.extend([
                {"term": {"source_country": c}},
                {"term": {"target_country": c}},
            ])

        try:
            resp = self.es.search(
                index=GDELT_INDEX_PATTERN,
                query={
                    "bool": {
                        "filter": [
                            {"range": {"date": {"gte": since}}},
                            {"bool": {"should": country_clauses, "minimum_should_match": 1}},
                        ],
                        "should": [
                            {"range": {"goldstein_scale": {"lt": -GOLDSTEIN_SEVERITY}}},
                            {"range": {"goldstein_scale": {"gt": GOLDSTEIN_SEVERITY}}},
                        ],
                        "minimum_should_match": 1,
                    }
                },
                size=10,
                sort=[{"goldstein_scale": "asc"}],
            )
            return [h["_source"] for h in resp["hits"]["hits"]]
        except Exception:
            return []

    def _build_correlation(
        self,
        case: dict[str, Any],
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        question = case.get("question", "")
        countries = case.get("countries_involved", [])
        price_now = case.get("outcome_yes_price", 0)
        change_24h = case.get("price_change_24h", 0) or 0
        change_7d = case.get("price_change_7d", 0) or 0
        change = change_24h if abs(change_24h) > abs(change_7d) else change_7d
        price_before = price_now - change

        worst_event = min(events, key=lambda e: e.get("goldstein_scale", 0))
        event_date = worst_event.get("date", "")
        case_date = case.get("date", "")

        # Determine direction: anticipation (market moved first) or reaction
        try:
            ev_dt = datetime.fromisoformat(str(event_date).replace("Z", "+00:00"))
            ca_dt = datetime.fromisoformat(str(case_date).replace("Z", "+00:00"))
            direction = "anticipation" if ca_dt < ev_dt else "reaction"
        except (ValueError, TypeError):
            direction = "unknown"

        severity = "medium" if direction == "anticipation" else "high"

        correlation_id = hashlib.sha256(
            f"{self.RULE_NAME}:{case.get('case_id', '')}:{now[:10]}".encode()
        ).hexdigest()[:20]

        return {
            "correlation_id": correlation_id,
            "timestamp": now,
            "date": now,
            "rule_name": self.RULE_NAME,
            "severity": severity,
            "countries_involved": countries,
            "diplomatic_event": {
                "event_id": worst_event.get("event_id", ""),
                "description": worst_event.get("cameo_description", ""),
                "goldstein": worst_event.get("goldstein_scale", 0),
            },
            "cyber_event": {"campaign_id": "", "apt_group": "", "techniques": []},
            "description": (
                f"Prediction market {direction}: \"{question}\" moved "
                f"{change:+.1%} (from {price_before:.0%} to {price_now:.0%}). "
                f"Concurrent GDELT event: Goldstein {worst_event.get('goldstein_scale', 0)} "
                f"— {worst_event.get('cameo_description', '')}."
            ),
            "timeline": [
                {"date": case_date or now, "type": "prediction_market",
                 "description": f"Polymarket: {change:+.1%} on \"{question[:80]}\""},
                {"date": event_date or now, "type": "geopolitical_event",
                 "description": f"GDELT: {worst_event.get('cameo_description', '')}"},
            ],
        }
