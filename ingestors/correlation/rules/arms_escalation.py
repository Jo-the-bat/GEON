"""Correlation Rule 7: Arms transfer + regional escalation.

Detects situations where a recent arms delivery to a country is followed
by a significant increase (>50%) in negative GDELT events involving the
recipient and its neighbours within a 90-day window.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from elasticsearch import Elasticsearch

from common.config import INDEX_PREFIX

logger = logging.getLogger(__name__)

TRANSFERS_INDEX = f"{INDEX_PREFIX}-arms-transfers"
GDELT_INDEX_PATTERN = f"{INDEX_PREFIX}-gdelt-events-*"
GOLDSTEIN_THRESHOLD: float = -3.0
ESCALATION_WINDOW_DAYS: int = 90
ESCALATION_THRESHOLD_PCT: float = 50.0

_NEIGHBORS_PATH = Path(__file__).resolve().parent.parent.parent / "common" / "country_neighbors.json"
_NEIGHBORS: dict[str, list[str]] = {}
try:
    with _NEIGHBORS_PATH.open() as f:
        _raw = json.load(f)
    _NEIGHBORS = {k: v for k, v in _raw.items() if k != "_comment"}
except Exception:
    pass


class ArmsEscalationRule:
    """Rule 7: Arms transfer followed by regional escalation."""

    RULE_NAME = "arms_transfer_escalation"

    def __init__(self, es: Elasticsearch, octi: Any = None) -> None:
        self.es = es

    def run(self) -> list[dict[str, Any]]:
        correlations: list[dict[str, Any]] = []

        transfers = self._find_recent_transfers()
        if not transfers:
            logger.info("[%s] No recent arms transfers found.", self.RULE_NAME)
            return correlations

        logger.info("[%s] Checking %d recent transfer(s).", self.RULE_NAME, len(transfers))

        # Group by recipient to avoid redundant queries
        by_recipient: dict[str, list[dict[str, Any]]] = {}
        for t in transfers:
            r = t.get("recipient_country", "")
            if r:
                by_recipient.setdefault(r, []).append(t)

        for recipient, txfers in by_recipient.items():
            neighbors = _NEIGHBORS.get(recipient, [])
            if not neighbors:
                continue

            # Use the earliest delivery in this batch as the reference point
            ref_date = min(
                t.get("date", "") for t in txfers
            )

            for neighbor in neighbors:
                before = self._count_negative_events(recipient, neighbor, ref_date, before=True)
                after = self._count_negative_events(recipient, neighbor, ref_date, before=False)

                if before == 0 and after == 0:
                    continue
                if before == 0:
                    pct_increase = 100.0 if after > 0 else 0
                else:
                    pct_increase = ((after - before) / before) * 100

                if pct_increase < ESCALATION_THRESHOLD_PCT:
                    continue

                severity = "critical" if pct_increase > 100 else "high"
                best_transfer = max(txfers, key=lambda t: t.get("tiv_value", 0))

                correlation = self._build_correlation(
                    recipient, neighbor, best_transfer,
                    before, after, pct_increase, severity,
                )
                correlations.append(correlation)

        logger.info("[%s] Generated %d correlation(s).", self.RULE_NAME, len(correlations))
        return correlations

    def _find_recent_transfers(self) -> list[dict[str, Any]]:
        """Find arms transfers delivered in the last 12 months."""
        try:
            resp = self.es.search(
                index=TRANSFERS_INDEX,
                query={
                    "bool": {
                        "filter": [
                            {"range": {"date": {"gte": "now-365d"}}},
                        ],
                        "must_not": [{"term": {"recipient_country": ""}}],
                    }
                },
                size=200,
                sort=[{"tiv_value": "desc"}],
            )
            return [h["_source"] for h in resp["hits"]["hits"]]
        except Exception:
            logger.exception("[%s] Failed to query arms transfers.", self.RULE_NAME)
            return []

    def _count_negative_events(
        self, country_a: str, country_b: str, ref_date: str, before: bool
    ) -> int:
        """Count GDELT negative events between two countries in a 90-day window."""
        try:
            dt = datetime.fromisoformat(ref_date.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            dt = datetime.now(timezone.utc)

        if before:
            start = (dt - timedelta(days=ESCALATION_WINDOW_DAYS)).isoformat()
            end = dt.isoformat()
        else:
            start = dt.isoformat()
            end = (dt + timedelta(days=ESCALATION_WINDOW_DAYS)).isoformat()

        try:
            resp = self.es.count(
                index=GDELT_INDEX_PATTERN,
                body={
                    "query": {
                        "bool": {
                            "filter": [
                                {"range": {"date": {"gte": start, "lte": end}}},
                                {"range": {"goldstein_scale": {"lt": GOLDSTEIN_THRESHOLD}}},
                                {"bool": {"should": [
                                    {"bool": {"must": [
                                        {"term": {"source_country": country_a}},
                                        {"term": {"target_country": country_b}},
                                    ]}},
                                    {"bool": {"must": [
                                        {"term": {"source_country": country_b}},
                                        {"term": {"target_country": country_a}},
                                    ]}},
                                ], "minimum_should_match": 1}},
                            ]
                        }
                    }
                },
            )
            return resp["count"]
        except Exception:
            return 0

    def _build_correlation(
        self,
        recipient: str,
        neighbor: str,
        transfer: dict[str, Any],
        before: int,
        after: int,
        pct_increase: float,
        severity: str,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        supplier = transfer.get("supplier_country", "")
        weapon = transfer.get("weapon_description", transfer.get("weapon_type", ""))

        correlation_id = hashlib.sha256(
            f"{self.RULE_NAME}:{recipient}:{neighbor}:{transfer.get('date', '')}".encode()
        ).hexdigest()[:20]

        return {
            "correlation_id": correlation_id,
            "timestamp": now,
            "date": now,
            "rule_name": self.RULE_NAME,
            "severity": severity,
            "countries_involved": sorted([recipient, neighbor]),
            "diplomatic_event": {
                "event_id": "",
                "description": (
                    f"Arms delivery to {recipient} ({weapon} from {supplier}, "
                    f"TIV {transfer.get('tiv_value', 0)}) followed by {pct_increase:.0f}% "
                    f"increase in negative events with neighbor {neighbor}"
                ),
                "goldstein": 0.0,
            },
            "cyber_event": {"campaign_id": "", "apt_group": "", "techniques": []},
            "description": (
                f"Arms transfer escalation: {supplier} delivered {weapon} to {recipient}. "
                f"Negative GDELT events between {recipient} and {neighbor} increased "
                f"{pct_increase:.0f}% (before: {before}, after: {after}) within {ESCALATION_WINDOW_DAYS}d."
            ),
            "timeline": [
                {"date": transfer.get("date", now), "type": "arms_transfer",
                 "description": f"{supplier} → {recipient}: {weapon}"},
                {"date": now, "type": "escalation",
                 "description": f"{pct_increase:.0f}% increase in {recipient}↔{neighbor} tensions"},
            ],
        }
