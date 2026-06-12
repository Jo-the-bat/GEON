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

from common.config import INDEX_PREFIX
from common.countries import normalize_country
from common.opencti_client import get_campaigns_by_country
from common.settings import setting
from elasticsearch import Elasticsearch
from pycti import OpenCTIApiClient

from correlation.scoring import (
    attribution_bonus,
    confidence,
    evidence_entry,
    volume_bonus,
)

logger = logging.getLogger(__name__)

OUTAGES_INDEX = f"{INDEX_PREFIX}-outages"
CTI_INDEX = f"{INDEX_PREFIX}-cti-threats"
APT_WINDOW_DAYS: int = setting("correlation.outage_apt.apt_window_days", 30)

_APT_MAPPING_PATH = (
    Path(__file__).resolve().parent.parent.parent / "common" / "country_apt_mapping.json"
)
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
            # Defensive: pre-migration outage docs may carry the old
            # Cloudflare spellings (e.g. "IVORY COAST").
            country = normalize_country(outage.get("country", ""))
            if not country:
                continue
            outage["country"] = country

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

    def _find_offensive_apts(self, country: str) -> list[dict[str, Any]]:
        """Find APT groups attributed to this country with recent activity."""
        known_apts = _COUNTRY_APT_MAP.get(country, [])
        if not known_apts:
            return []

        # Check OpenCTI for recent campaigns — validated against the
        # known attribution map (same strict validation as Rules 1/3/6;
        # unvalidated OpenCTI matches cross-contaminate attributions).
        if self.octi:
            try:
                campaigns = get_campaigns_by_country(
                    self.octi, country, days_back=APT_WINDOW_DAYS
                )
                known_lower = {a.lower() for a in known_apts}
                validated = [
                    c for c in campaigns or []
                    if c.get("name", "").lower() in known_lower
                ]
                if validated:
                    return [{"name": c.get("name", ""), "type": "offensive",
                             "id": c.get("id", ""), "source": "opencti",
                             "date": str(c.get("modified", c.get("created", "")))}
                            for c in validated]
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
                         "id": h["_id"], "source": "es_cti",
                         "date": str(h["_source"].get("modified",
                                                      h["_source"].get("created", "")))}
                        for h in resp["hits"]["hits"]]
        except Exception:
            pass

        # Fallback: return static attribution
        return [{"name": apt, "type": "offensive", "id": "", "source": "static"}
                for apt in known_apts[:3]]

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
                     "id": h["_id"], "source": "es_cti",
                     "date": str(h["_source"].get("modified",
                                                  h["_source"].get("created", "")))}
                    for h in resp["hits"]["hits"]]
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
        primary_apt = all_apts[0] if all_apts else {}

        correlation_id = hashlib.sha256(
            f"{self.RULE_NAME}:{country}:{outage.get('start_time', '')}".encode()
        ).hexdigest()[:20]

        desc_parts = [
            f"Internet outage in {country} "
            f"({outage.get('severity', 'unknown')}, {outage.get('type', '')})"
        ]
        if offensive_apts:
            names = ", ".join(a["name"] for a in offensive_apts[:3])
            desc_parts.append(
                f"coincides with APT groups attributed to {country}: {names}"
            )
        if targeting_apts:
            names = ", ".join(a["name"] for a in targeting_apts[:3])
            desc_parts.append(f"APT groups targeting {country}: {names}")

        # Evidence: the outage doc + the offensive/targeting APT entries
        # (each carries its provenance: opencti / es_cti / static).
        evidence: list[dict[str, str]] = [
            evidence_entry(
                index=outage.get("_es_index") or OUTAGES_INDEX,
                doc_id=outage.get("_es_id") or outage.get("outage_id", ""),
                date=str(outage.get("start_time", "")),
                kind="outage",
                summary=(f"Internet outage in {country}: "
                         f"{outage.get('severity', '')} / {outage.get('type', '')}"),
            ),
        ]
        for apt in all_apts[:5]:
            source = apt.get("source", "static")
            index = {"opencti": "opencti", "es_cti": CTI_INDEX}.get(source, source)
            evidence.append(evidence_entry(
                index=index,
                doc_id=apt.get("id", "") or apt.get("name", ""),
                date=str(apt.get("date", "")),
                kind="cyber",
                summary=f"{apt.get('name', 'Unknown')} ({apt.get('type', '')}, "
                        f"{source} attribution)",
            ))

        # Confidence: best attribution provenance present across the
        # matched APT entries + corroboration volume.
        best_attribution = max(
            (attribution_bonus(a.get("source", "static")) for a in all_apts),
            default=0.0,
        )
        conf, factors = confidence(30, {
            "attribution": best_attribution,
            "volume": volume_bonus(len(all_apts)),
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
                "event_id": outage.get("outage_id", ""),
                "description": (
                    f"Internet outage: {outage.get('severity', '')} / "
                    f"{outage.get('duration_hours', 'unknown')}h"
                ),
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
                 "description": (f"Outage in {country}: "
                                 f"{outage.get('severity', '')} / {outage.get('type', '')}")},
            ] + [
                {"date": now, "type": "apt_activity",
                 "description": f"APT: {a['name']} ({a['type']})"} for a in all_apts[:3]
            ],
        }
