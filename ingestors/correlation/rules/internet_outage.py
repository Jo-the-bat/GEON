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

from common.config import INDEX_PREFIX
from common.countries import normalize_country
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

WINDOW_HOURS: int = setting("correlation.internet_outage.window_hours", 48)
GOLDSTEIN_THRESHOLD: float = setting(
    "correlation.internet_outage.goldstein_threshold", -5.0)
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
            # Defensive: pre-migration outage docs may carry the old
            # Cloudflare spellings (e.g. "IVORY COAST").
            country = normalize_country(outage.get("country", ""))
            if not country:
                continue
            outage["country"] = country

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
            # Keep the ES identity alongside the source so the
            # correlation can reference the outage as evidence.
            outages = []
            for h in resp["hits"]["hits"]:
                src = h["_source"]
                src["_es_id"] = h.get("_id", "")
                src["_es_index"] = h.get("_index", "")
                outages.append(src)
            return outages
        except Exception:
            logger.exception("[%s] Failed to query outages.", self.RULE_NAME)
            return []

    def _find_gdelt_escalation(
        self, country: str, ref_time: str
    ) -> list[dict[str, Any]]:
        """Find GDELT events with Goldstein < threshold near the outage.

        Returns:
            Raw ES hits (``_index``/``_id``/``_source``) so the
            correlation can reference its evidence.
        """
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
            return resp["hits"]["hits"]
        except Exception:
            return []

    def _find_acled_conflict(
        self, country: str, ref_time: str
    ) -> list[dict[str, Any]]:
        """Find ACLED conflict events near the outage (raw ES hits)."""
        window_start, window_end = self._time_window(ref_time)
        try:
            resp = self.es.search(
                index=ACLED_INDEX_PATTERN,
                query={
                    "bool": {
                        "filter": [
                            {"range": {"event_date": {"gte": window_start, "lte": window_end}}},
                            {"term": {"country": country}},
                        ]
                    }
                },
                size=10,
            )
            return resp["hits"]["hits"]
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

        # Accept both raw ES hits (with _source) and bare source dicts so
        # the signature stays backward compatible.
        gdelt_events = [h.get("_source", h) for h in gdelt_hits]
        acled_events = [h.get("_source", h) for h in acled_hits]

        is_total = outage.get("severity") == "total"
        has_conflict = bool(acled_events)
        has_diplomatic = bool(gdelt_events)

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
        worst_event_date = ""
        if gdelt_events:
            worst = min(gdelt_events, key=lambda e: e.get("goldstein_scale", 0))
            worst_goldstein = worst.get("goldstein_scale")
            worst_event_desc = worst.get("cameo_description", "")
            worst_event_date = worst.get("date", "")

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
        for evt in gdelt_events[:3]:
            timeline.append({
                "date": evt.get("date", now),
                "type": "diplomatic",
                "description": (f"Goldstein {evt.get('goldstein_scale')}: "
                                f"{evt.get('cameo_description', '')}"),
            })
        for evt in acled_events[:3]:
            timeline.append({
                "date": evt.get("event_date", now),
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
            desc_parts.append(f"and {len(acled_events)} ACLED conflict event(s)")

        # Evidence: the outage doc + the corroborating GDELT/ACLED hits,
        # so an analyst can verify the claim against the sources.
        evidence: list[dict[str, str]] = [
            evidence_entry(
                index=outage.get("_es_index") or OUTAGES_INDEX,
                doc_id=outage.get("_es_id") or outage.get("outage_id", ""),
                date=str(outage.get("start_time", "")),
                kind="outage",
                summary=(f"{outage.get('severity', 'unknown')} internet outage "
                         f"in {country} ({outage.get('type', '')})"),
            ),
        ]
        for hit in gdelt_hits[:5]:
            src = hit.get("_source", hit)
            evidence.append(evidence_from_hit(
                hit, "diplomatic",
                f"Goldstein {src.get('goldstein_scale')}: "
                f"{src.get('cameo_description', '')}",
            ))
        for hit in acled_hits[:5]:
            src = hit.get("_source", hit)
            evidence.append(evidence_from_hit(
                hit, "conflict",
                f"ACLED: {src.get('event_type', '')} — {src.get('notes', '')[:100]}",
                date_field="event_date",
            ))

        # Confidence: corroboration volume + temporal proximity between
        # the outage start and the worst GDELT escalation.
        conf, factors = confidence(30, {
            "volume": volume_bonus(len(gdelt_events) + len(acled_events)),
            "proximity": proximity_bonus(
                self._days_apart(outage.get("start_time", ""), worst_event_date),
                WINDOW_HOURS / 24,
            ),
        })

        return {
            "correlation_id": correlation_id,
            "timestamp": now,
            "date": now,
            "rule_name": self.RULE_NAME,
            "severity": severity,
            "countries_involved": [country],
            "confidence": conf,
            "confidence_factors": factors,
            "evidence": evidence,
            "diplomatic_event": {
                "event_id": gdelt_events[0].get("event_id", "") if gdelt_events else "",
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
    def _days_apart(time_a: str, time_b: str) -> float | None:
        """Absolute gap in days between two ISO timestamps (None if unparseable)."""
        try:
            dt_a = datetime.fromisoformat(str(time_a).replace("Z", "+00:00"))
            dt_b = datetime.fromisoformat(str(time_b).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
        if dt_a.tzinfo is None:
            dt_a = dt_a.replace(tzinfo=timezone.utc)
        if dt_b.tzinfo is None:
            dt_b = dt_b.replace(tzinfo=timezone.utc)
        return abs((dt_a - dt_b).total_seconds()) / 86400.0

    @staticmethod
    def _time_window(ref_time: str) -> tuple[str, str]:
        try:
            dt = datetime.fromisoformat(ref_time.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            dt = datetime.now(timezone.utc)
        start = (dt - timedelta(hours=WINDOW_HOURS)).isoformat()
        end = (dt + timedelta(hours=WINDOW_HOURS)).isoformat()
        return start, end
