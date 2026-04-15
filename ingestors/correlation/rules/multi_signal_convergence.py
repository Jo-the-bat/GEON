"""Correlation Rule 10: Multi-signal convergence (CRITICAL).

The most important rule. Detects when 3+ independent signals converge
on the same country within the same temporal window, producing an
intelligence fusion alert.

Signals checked (7 total):
  1. GDELT — >100 negative events (Goldstein < -3) in 7 days
  2. Sanctions — new sanction indexed in 30 days
  3. Internet outage — outage in geon-outages in 7 days
  4. Prediction market — Polymarket case with >5% movement in 7 days
  5. APT activity — diplomatic_escalation_apt correlation in 30 days
  6. ACLED conflict — events in 7 days
  7. Military spending increase — in geon-military-spending
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from elasticsearch import Elasticsearch

from common.config import INDEX_PREFIX

logger = logging.getLogger(__name__)

RISK_SCORES_INDEX = f"{INDEX_PREFIX}-risk-scores"
GDELT_INDEX_PATTERN = f"{INDEX_PREFIX}-gdelt-events-*"
SANCTIONS_INDEX = f"{INDEX_PREFIX}-sanctions"
OUTAGES_INDEX = f"{INDEX_PREFIX}-outages"
POLYMARKET_INDEX = f"{INDEX_PREFIX}-polymarket-cases"
CORRELATIONS_INDEX = f"{INDEX_PREFIX}-correlations"
ACLED_INDEX_PATTERN = f"{INDEX_PREFIX}-acled-*"
SPENDING_INDEX = f"{INDEX_PREFIX}-military-spending"

RISK_SCORE_THRESHOLD: float = 40.0
GDELT_NEGATIVE_THRESHOLD: int = 100
GOLDSTEIN_THRESHOLD: float = -3.0
PREDICTION_SHIFT_THRESHOLD: float = 0.05
SPENDING_YOY_THRESHOLD: float = 10.0
MIN_SIGNALS: int = 3


class MultiSignalConvergenceRule:
    """Rule 10: Multi-signal convergence fusion."""

    RULE_NAME = "multi_signal_convergence"

    def __init__(self, es: Elasticsearch, octi: Any = None) -> None:
        self.es = es

    def run(self) -> list[dict[str, Any]]:
        correlations: list[dict[str, Any]] = []

        high_risk_countries = self._get_high_risk_countries()
        if not high_risk_countries:
            logger.info("[%s] No countries above risk threshold %.0f.", self.RULE_NAME, RISK_SCORE_THRESHOLD)
            return correlations

        logger.info(
            "[%s] Checking %d high-risk countries for signal convergence.",
            self.RULE_NAME, len(high_risk_countries),
        )

        for risk_doc in high_risk_countries:
            country = risk_doc.get("country", "")
            if not country:
                continue

            signals = self._check_all_signals(country)
            active_count = sum(1 for v in signals.values() if v)

            if active_count < MIN_SIGNALS:
                continue

            correlation = self._build_correlation(country, risk_doc, signals, active_count)
            correlations.append(correlation)

        logger.info("[%s] Generated %d convergence alert(s).", self.RULE_NAME, len(correlations))
        return correlations

    def _get_high_risk_countries(self) -> list[dict[str, Any]]:
        """Get countries with risk_score > threshold."""
        try:
            resp = self.es.search(
                index=RISK_SCORES_INDEX,
                query={"range": {"risk_score": {"gt": RISK_SCORE_THRESHOLD}}},
                size=100,
                sort=[{"risk_score": "desc"}],
            )
            return [h["_source"] for h in resp["hits"]["hits"]]
        except Exception:
            logger.exception("[%s] Failed to query risk scores.", self.RULE_NAME)
            return []

    def _check_all_signals(self, country: str) -> dict[str, Any]:
        """Check all 7 signals for a country. Returns dict of signal results."""
        return {
            "gdelt_negative_events": self._signal_gdelt(country),
            "sanctions_recent": self._signal_sanctions(country),
            "internet_outage": self._signal_outage(country),
            "prediction_market_movement": self._signal_prediction(country),
            "apt_activity": self._signal_apt(country),
            "acled_conflicts": self._signal_acled(country),
            "military_spending_increase": self._signal_spending(country),
        }

    # ------------------------------------------------------------------
    # Signal 1: GDELT negative events
    # ------------------------------------------------------------------
    def _signal_gdelt(self, country: str) -> int | bool:
        """Count GDELT negative events (Goldstein < -3) in last 7 days."""
        try:
            resp = self.es.count(
                index=GDELT_INDEX_PATTERN,
                body={
                    "query": {
                        "bool": {
                            "filter": [
                                {"range": {"date": {"gte": "now-7d"}}},
                                {"range": {"goldstein_scale": {"lt": GOLDSTEIN_THRESHOLD}}},
                                {"bool": {"should": [
                                    {"term": {"source_country": country}},
                                    {"term": {"target_country": country}},
                                ], "minimum_should_match": 1}},
                            ]
                        }
                    }
                },
            )
            count = resp["count"]
            return count if count >= GDELT_NEGATIVE_THRESHOLD else 0
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # Signal 2: Recent sanctions
    # ------------------------------------------------------------------
    def _signal_sanctions(self, country: str) -> bool:
        try:
            resp = self.es.count(
                index=SANCTIONS_INDEX,
                body={
                    "query": {
                        "bool": {
                            "filter": [
                                {"range": {"date": {"gte": "now-30d"}}},
                                {"term": {"country": country}},
                            ]
                        }
                    }
                },
            )
            return resp["count"] > 0
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Signal 3: Internet outage
    # ------------------------------------------------------------------
    def _signal_outage(self, country: str) -> bool:
        try:
            resp = self.es.count(
                index=OUTAGES_INDEX,
                body={
                    "query": {
                        "bool": {
                            "filter": [
                                {"range": {"start_time": {"gte": "now-7d"}}},
                                {"term": {"country": country}},
                            ]
                        }
                    }
                },
            )
            return resp["count"] > 0
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Signal 4: Prediction market movement
    # ------------------------------------------------------------------
    def _signal_prediction(self, country: str) -> str | bool:
        try:
            resp = self.es.search(
                index=POLYMARKET_INDEX,
                query={
                    "bool": {
                        "filter": [
                            {"term": {"status": "active"}},
                            {"term": {"countries_involved": country}},
                        ],
                        "should": [
                            {"range": {"price_change_7d": {"gt": PREDICTION_SHIFT_THRESHOLD}}},
                            {"range": {"price_change_7d": {"lt": -PREDICTION_SHIFT_THRESHOLD}}},
                        ],
                        "minimum_should_match": 1,
                    }
                },
                size=1,
                sort=[{"volume": "desc"}],
            )
            hits = resp["hits"]["hits"]
            if hits:
                src = hits[0]["_source"]
                change = src.get("price_change_7d", 0) or 0
                question = src.get("question", "")[:60]
                return f"{change:+.0%} on '{question}'"
            return False
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Signal 5: APT correlation
    # ------------------------------------------------------------------
    def _signal_apt(self, country: str) -> str | bool:
        try:
            resp = self.es.search(
                index=CORRELATIONS_INDEX,
                query={
                    "bool": {
                        "filter": [
                            {"range": {"date": {"gte": "now-30d"}}},
                            {"term": {"countries_involved": country}},
                            {"terms": {"rule_name": [
                                "diplomatic_escalation_apt",
                                "outage_apt_activity",
                                "military_buildup_cyber",
                            ]}},
                        ]
                    }
                },
                size=5,
            )
            hits = resp["hits"]["hits"]
            if hits:
                apt_names = set()
                for h in hits:
                    name = h["_source"].get("cyber_event", {}).get("apt_group", "")
                    if name:
                        apt_names.add(name)
                return ", ".join(sorted(apt_names)) if apt_names else True
            return False
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Signal 6: ACLED conflicts
    # ------------------------------------------------------------------
    def _signal_acled(self, country: str) -> int | bool:
        try:
            resp = self.es.count(
                index=ACLED_INDEX_PATTERN,
                body={
                    "query": {
                        "bool": {
                            "filter": [
                                {"range": {"date": {"gte": "now-7d"}}},
                                {"term": {"country": country}},
                            ]
                        }
                    }
                },
            )
            count = resp["count"]
            return count if count > 0 else 0
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # Signal 7: Military spending increase
    # ------------------------------------------------------------------
    def _signal_spending(self, country: str) -> bool:
        try:
            resp = self.es.search(
                index=SPENDING_INDEX,
                query={"term": {"country": country}},
                size=1,
                sort=[{"year": "desc"}],
            )
            hits = resp["hits"]["hits"]
            if hits:
                yoy = hits[0]["_source"].get("spending_change_yoy_pct", 0)
                return yoy > SPENDING_YOY_THRESHOLD
            return False
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Build correlation document
    # ------------------------------------------------------------------
    def _build_correlation(
        self,
        country: str,
        risk_doc: dict[str, Any],
        signals: dict[str, Any],
        active_count: int,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        window_start = (now - timedelta(days=7)).strftime("%Y-%m-%d")
        window_end = now.strftime("%Y-%m-%d")

        if active_count >= 5:
            severity = "critical"
            flag = "intelligence_fusion_alert"
        elif active_count >= 4:
            severity = "critical"
            flag = ""
        else:
            severity = "high"
            flag = ""

        risk_score = risk_doc.get("risk_score", 0)

        # Build narrative
        active_names = []
        if signals["gdelt_negative_events"]:
            active_names.append(f"{signals['gdelt_negative_events']} GDELT negative events")
        if signals["sanctions_recent"]:
            active_names.append("recent sanctions")
        if signals["internet_outage"]:
            active_names.append("internet outage")
        if signals["prediction_market_movement"]:
            active_names.append(f"prediction market ({signals['prediction_market_movement']})")
        if signals["apt_activity"]:
            active_names.append(f"APT activity ({signals['apt_activity']})")
        if signals["acled_conflicts"]:
            active_names.append(f"{signals['acled_conflicts']} ACLED conflicts")
        if signals["military_spending_increase"]:
            active_names.append("military spending increase")

        narrative = (
            f"{country}: {active_count} convergent signals detected "
            f"({window_start} to {window_end}) — {'; '.join(active_names)}. "
            f"Risk score: {risk_score}."
        )

        correlation_id = hashlib.sha256(
            f"{self.RULE_NAME}:{country}:{window_end}".encode()
        ).hexdigest()[:20]

        doc: dict[str, Any] = {
            "correlation_id": correlation_id,
            "timestamp": now.isoformat(),
            "date": now.isoformat(),
            "rule_name": self.RULE_NAME,
            "severity": severity,
            "countries_involved": [country],
            "diplomatic_event": {
                "event_id": "",
                "description": narrative,
                "goldstein": 0.0,
            },
            "cyber_event": {
                "campaign_id": "",
                "apt_group": str(signals.get("apt_activity", "")) if signals.get("apt_activity") else "",
                "techniques": [],
            },
            "description": narrative,
            "timeline": [],
            # Extended fields for multi-signal convergence
            "signals_active": active_count,
            "signals_detail": {
                "gdelt_negative_events": signals["gdelt_negative_events"] or 0,
                "sanctions_recent": bool(signals["sanctions_recent"]),
                "internet_outage": bool(signals["internet_outage"]),
                "prediction_market_movement": str(signals["prediction_market_movement"]) if signals["prediction_market_movement"] else "",
                "apt_activity": str(signals["apt_activity"]) if signals["apt_activity"] else "",
                "acled_conflicts": signals["acled_conflicts"] or 0,
                "military_spending_increase": bool(signals["military_spending_increase"]),
            },
            "risk_score": risk_score,
            "window": f"{window_start} to {window_end}",
            "narrative": narrative,
        }
        if flag:
            doc["flag"] = flag

        return doc
