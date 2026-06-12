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

from common.config import INDEX_PREFIX
from common.settings import setting
from elasticsearch import Elasticsearch

from correlation.scoring import (
    confidence,
    evidence_entry,
    evidence_from_hit,
    proximity_bonus,
    volume_bonus,
)

logger = logging.getLogger(__name__)

POLYMARKET_INDEX = f"{INDEX_PREFIX}-polymarket-cases"
GDELT_INDEX_PATTERN = f"{INDEX_PREFIX}-gdelt-events-*"
PRICE_SHIFT_THRESHOLD: float = setting(
    "correlation.prediction_validated.price_shift_threshold", 0.10)  # 10%
GOLDSTEIN_SEVERITY: float = setting(
    "correlation.prediction_validated.goldstein_severity", 7.0)
WINDOW_HOURS: int = setting("correlation.prediction_validated.window_hours", 72)


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

        logger.info("[%s] Checking %d market(s) with significant movement.",
                    self.RULE_NAME, len(movers))

        for case in movers:
            countries = case.get("countries_involved", [])
            if not countries:
                continue

            event_hits = self._find_high_severity_events(countries)
            if not event_hits:
                continue

            events = [h["_source"] for h in event_hits]
            correlation = self._build_correlation(case, events, event_hits=event_hits)
            correlations.append(correlation)

        logger.info("[%s] Generated %d correlation(s).", self.RULE_NAME, len(correlations))
        return correlations

    def _find_significant_movers(self) -> list[dict[str, Any]]:
        """Find Polymarket cases with >10% price change in recent window.

        Cases whose ``price_change_24h`` / ``price_change_7d`` is ``null``
        (insufficient price history) are excluded — applying a threshold to a
        missing value would otherwise treat ``null`` as ``0.0`` and silently
        drop (or wrongly include) the case.
        """
        try:
            # A should-clause matches when either the 24h or the 7d window
            # has a value AND exceeds the threshold. The `exists` filter on
            # each sub-clause is what keeps null-valued cases out.
            resp = self.es.search(
                index=POLYMARKET_INDEX,
                query={
                    "bool": {
                        "filter": [
                            {"term": {"status": "active"}},
                            {"range": {"date": {"gte": "now-7d"}}},
                        ],
                        "should": [
                            {"bool": {"must": [
                                {"exists": {"field": "price_change_24h"}},
                                {"range": {"price_change_24h": {"gt": PRICE_SHIFT_THRESHOLD}}},
                            ]}},
                            {"bool": {"must": [
                                {"exists": {"field": "price_change_24h"}},
                                {"range": {"price_change_24h": {"lt": -PRICE_SHIFT_THRESHOLD}}},
                            ]}},
                            {"bool": {"must": [
                                {"exists": {"field": "price_change_7d"}},
                                {"range": {"price_change_7d": {"gt": PRICE_SHIFT_THRESHOLD}}},
                            ]}},
                            {"bool": {"must": [
                                {"exists": {"field": "price_change_7d"}},
                                {"range": {"price_change_7d": {"lt": -PRICE_SHIFT_THRESHOLD}}},
                            ]}},
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
        """Find GDELT events with |Goldstein| > threshold for given countries in 72h.

        Returns:
            RAW ES hits (``_index``/``_id`` kept) so the correlation can
            reference its evidence documents.
        """
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
            return resp["hits"]["hits"]
        except Exception:
            return []

    def _build_correlation(
        self,
        case: dict[str, Any],
        events: list[dict[str, Any]],
        event_hits: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Assemble a correlation document.

        Args:
            case: Polymarket case source document.
            events: GDELT event source dicts (``_source`` contents).
            event_hits: Raw ES hits (with ``_index``/``_id``) matching
                *events*, used for evidence references when available.
        """
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
        days_apart: float | None
        try:
            ev_dt = datetime.fromisoformat(str(event_date).replace("Z", "+00:00"))
            ca_dt = datetime.fromisoformat(str(case_date).replace("Z", "+00:00"))
            direction = "anticipation" if ca_dt < ev_dt else "reaction"
            days_apart = abs((ev_dt - ca_dt).total_seconds()) / 86400.0
        except (ValueError, TypeError):
            direction = "unknown"
            days_apart = None

        severity = "medium" if direction == "anticipation" else "high"

        # Stable identity anchored on the market case and the validating
        # GDELT event (previously the current date rotated the id daily).
        event_anchor = worst_event.get("event_id", "") or str(event_date)
        correlation_id = hashlib.sha256(
            f"{self.RULE_NAME}:{case.get('case_id', '')}:{event_anchor}".encode()
        ).hexdigest()[:20]

        # Evidence: the market case + the validating GDELT events, so the
        # analyst can verify the claim instead of trusting the alert.
        evidence: list[dict[str, str]] = [
            evidence_entry(
                index=POLYMARKET_INDEX,
                doc_id=str(case.get("case_id", "")),
                date=str(case_date),
                kind="market",
                summary=f"{change:+.1%} on \"{question[:120]}\"",
            ),
        ]
        if event_hits:
            for hit in event_hits[:5]:
                src = hit.get("_source", {})
                evidence.append(evidence_from_hit(
                    hit, "diplomatic",
                    f"Goldstein {src.get('goldstein_scale')}: "
                    f"{src.get('cameo_description', '')}",
                ))
        else:
            # Direct callers (tests, backtesting) may only have sources.
            for ev in events[:5]:
                evidence.append(evidence_entry(
                    index=GDELT_INDEX_PATTERN,
                    doc_id=str(ev.get("event_id", "")),
                    date=str(ev.get("date", "")),
                    kind="diplomatic",
                    summary=f"Goldstein {ev.get('goldstein_scale')}: "
                            f"{ev.get('cameo_description', '')}",
                ))

        # Confidence: corroborating event volume + temporal proximity of
        # the market move and the event + magnitude of the price shift.
        conf, factors = confidence(30, {
            "volume": volume_bonus(len(events)),
            "proximity": proximity_bonus(days_apart, WINDOW_HOURS / 24.0),
            "market_move": max(
                0.0, min(15.0, (abs(change) - PRICE_SHIFT_THRESHOLD) * 100)),
        })

        return {
            "correlation_id": correlation_id,
            "timestamp": now,
            "date": now,
            "rule_name": self.RULE_NAME,
            "severity": severity,
            "countries_involved": countries,
            "confidence": conf,
            "confidence_factors": factors,
            "evidence": evidence,
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
