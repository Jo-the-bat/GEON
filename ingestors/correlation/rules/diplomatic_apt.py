"""Correlation Rule 1: Diplomatic escalation + APT activity.

Detects situations where a significant diplomatic escalation (Goldstein
score < -5) between two countries coincides with APT campaigns attributed
to either country within a +/-30-day window.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from common.config import INDEX_PREFIX
from common.opencti_client import get_campaigns_by_country
from common.settings import setting
from elasticsearch import Elasticsearch
from pycti import OpenCTIApiClient

from correlation.baselines import Baseline, negative_events_baseline
from correlation.scoring import (
    attribution_bonus,
    confidence,
    evidence_entry,
    evidence_from_hit,
    volume_bonus,
    zscore_bonus,
)

# Load country → APT attribution mapping for validation.
_MAPPING_PATH = (
    Path(__file__).resolve().parent.parent.parent / "common" / "country_apt_mapping.json"
)
_COUNTRY_APT_MAP: dict[str, list[str]] = {}
try:
    with _MAPPING_PATH.open() as f:
        _raw = json.load(f)
    _COUNTRY_APT_MAP = {k: [a.lower() for a in v] for k, v in _raw.items() if k != "_comment"}
except Exception:
    pass  # Mapping file missing — skip validation

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
GOLDSTEIN_THRESHOLD: float = setting(
    "correlation.diplomatic_apt.goldstein_threshold", -5.0)
LOOKBACK_DAYS: int = setting("correlation.diplomatic_apt.lookback_days", 7)
APT_WINDOW_DAYS: int = setting("correlation.diplomatic_apt.apt_window_days", 30)
# Statistical gating: a pair only fires when its negative-event rate
# deviates from ITS OWN baseline (z-score) — chronically tense pairs no
# longer alert on their normal background level. Pairs without enough
# history fall back to the absolute-threshold behaviour.
USE_BASELINE: bool = setting("correlation.diplomatic_apt.use_baseline", True)
ZSCORE_THRESHOLD: float = setting(
    "correlation.diplomatic_apt.zscore_threshold", 2.0)
GDELT_INDEX_PATTERN = f"{INDEX_PREFIX}-gdelt-events-*"


class DiplomaticAPTRule:
    """Rule 1: Diplomatic escalation coinciding with APT activity.

    Queries Elasticsearch for GDELT events with Goldstein scores below
    the threshold, groups them by country pairs, then queries OpenCTI for
    APT campaigns attributed to either country in a +/-30-day window.

    Attributes:
        es: Elasticsearch client.
        octi: OpenCTI API client.
        lookback_days: Number of days to look back in GDELT data.
    """

    RULE_NAME = "diplomatic_escalation_apt"

    def __init__(
        self,
        es: Elasticsearch,
        octi: OpenCTIApiClient,
        lookback_days: int = LOOKBACK_DAYS,
        as_of: datetime | None = None,
    ) -> None:
        self.es = es
        self.octi = octi
        self.lookback_days = lookback_days
        # Clock override for the backtesting harness.
        self.as_of = as_of

    def _now(self) -> datetime:
        return self.as_of or datetime.now(timezone.utc)

    def run(self) -> list[dict[str, Any]]:
        """Execute the rule and return a list of correlation documents.

        Returns:
            List of correlation dicts ready for indexing. Each dict follows
            the ``geon-correlations`` schema.
        """
        correlations: list[dict[str, Any]] = []

        # Step 1: Find severe diplomatic events.
        country_pairs = self._find_escalations()
        if not country_pairs:
            logger.info("[%s] No diplomatic escalations found.", self.RULE_NAME)
            return correlations

        logger.info(
            "[%s] Found escalations for %d country pair(s).",
            self.RULE_NAME,
            len(country_pairs),
        )

        # Step 2: For each country pair, look for APT activity.
        for pair_key, event_hits in country_pairs.items():
            src_country, tgt_country = pair_key.split("||")

            # Statistical gating against the pair's own history.
            baseline = self._pair_baseline(src_country, tgt_country)
            if (
                USE_BASELINE
                and baseline is not None
                and baseline.zscore < ZSCORE_THRESHOLD
            ):
                logger.debug(
                    "[%s] %s below baseline deviation (z=%.1f < %.1f) — skipped.",
                    self.RULE_NAME, pair_key, baseline.zscore, ZSCORE_THRESHOLD,
                )
                continue

            events = [h["_source"] for h in event_hits]
            worst_event = min(events, key=lambda e: e["goldstein_scale"])

            apt_matches = self._find_apt_activity(src_country, tgt_country, worst_event)
            if not apt_matches:
                continue

            # Step 3: Build the correlation document.
            correlation = self._build_correlation(
                src_country, tgt_country, worst_event, apt_matches,
                event_hits=event_hits, baseline=baseline,
            )
            correlations.append(correlation)

        logger.info(
            "[%s] Generated %d correlation(s).", self.RULE_NAME, len(correlations)
        )
        return correlations

    def _pair_baseline(self, country_a: str, country_b: str) -> Baseline | None:
        """Compute the pair's negative-event baseline (None = no history)."""
        if not USE_BASELINE:
            return None
        return negative_events_baseline(
            self.es,
            country_a,
            country_b,
            goldstein_lt=GOLDSTEIN_THRESHOLD,
            window_days=self.lookback_days,
            as_of=self.as_of,
        )

    # ------------------------------------------------------------------
    # Step 1: Query GDELT for diplomatic escalations
    # ------------------------------------------------------------------

    def _find_escalations(self) -> dict[str, list[dict[str, Any]]]:
        """Query ES for GDELT events with Goldstein < threshold.

        Returns:
            Dict mapping ``"source_country||target_country"`` keys to lists
            of matching event dicts.
        """
        since = (self._now() - timedelta(days=self.lookback_days)).isoformat()

        query: dict[str, Any] = {
            "bool": {
                "must": [
                    {"range": {"goldstein_scale": {"lt": GOLDSTEIN_THRESHOLD}}},
                    {"range": {"date": {
                        "gte": since,
                        "lte": self._now().isoformat(),
                    }}},
                ],
                "must_not": [
                    # Ignore events without country data.
                    {"term": {"source_country": ""}},
                    {"term": {"target_country": ""}},
                ],
            }
        }

        resp = self.es.search(
            index=GDELT_INDEX_PATTERN,
            query=query,
            size=500,
            sort=[{"goldstein_scale": "asc"}],
        )

        hits = resp.get("hits", {}).get("hits", [])
        logger.debug("[%s] GDELT query returned %d hits.", self.RULE_NAME, len(hits))

        # Group RAW hits by country pair (order-independent) — _index/_id
        # are kept so the correlation can reference its evidence.
        pairs: dict[str, list[dict[str, Any]]] = {}
        for hit in hits:
            src = hit["_source"]
            pair = self._pair_key(src["source_country"], src["target_country"])
            pairs.setdefault(pair, []).append(hit)

        return pairs

    # ------------------------------------------------------------------
    # Step 2: Query OpenCTI for APT activity
    # ------------------------------------------------------------------

    def _find_apt_activity(
        self,
        country_a: str,
        country_b: str,
        reference_event: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Query OpenCTI for campaigns linked to either country.

        Uses a +/- APT_WINDOW_DAYS window around the reference event date.

        Args:
            country_a: First country in the pair.
            country_b: Second country in the pair.
            reference_event: The most severe GDELT event for this pair.

        Returns:
            List of matching campaign/intrusion-set dicts from OpenCTI.
        """
        matches: list[dict[str, Any]] = []

        for country in [country_a, country_b]:
            campaigns = get_campaigns_by_country(
                self.octi, country, days_back=APT_WINDOW_DAYS
            )
            if campaigns:
                matches.extend(campaigns)

        # Defence-in-depth validation: get_campaigns_by_country is already
        # STIX-relationship filtered (originates-from / attributed-to a
        # Location), but the static attribution map remains a second
        # gate against mis-modelled relationships. If neither country has
        # entries in the mapping, we cannot validate — return empty to
        # avoid false positives.
        if _COUNTRY_APT_MAP:
            known_apts: set[str] = set()
            for c in [country_a, country_b]:
                known_apts.update(_COUNTRY_APT_MAP.get(c.upper(), []))
            validated = [
                m for m in matches
                if m.get("name", "").lower() in known_apts
            ]
            logger.debug(
                "APT validation: %d/%d matches confirmed for %s/%s.",
                len(validated), len(matches), country_a, country_b,
            )
            matches = validated

        return matches

    # ------------------------------------------------------------------
    # Step 3: Build the correlation document
    # ------------------------------------------------------------------

    def _build_correlation(
        self,
        country_a: str,
        country_b: str,
        worst_event: dict[str, Any],
        apt_matches: list[dict[str, Any]],
        event_hits: list[dict[str, Any]] | None = None,
        baseline: Baseline | None = None,
    ) -> dict[str, Any]:
        """Assemble a correlation document.

        Args:
            country_a: Source country.
            country_b: Target country.
            worst_event: The GDELT event with the lowest Goldstein score.
            apt_matches: List of matching APT campaigns/intrusion sets.
            event_hits: Raw ES hits (with _index/_id) for evidence refs.
            baseline: Statistical baseline of the pair, when available.

        Returns:
            Correlation document dict following the ``geon-correlations``
            schema.
        """
        now = self._now().isoformat()

        # Determine severity based on Goldstein score and APT confidence.
        goldstein = worst_event.get("goldstein_scale", 0)
        severity = self._compute_severity(goldstein, apt_matches)

        # Pick the most relevant APT match for the summary.
        primary_apt = apt_matches[0] if apt_matches else {}
        apt_name = primary_apt.get("name", "Unknown APT")
        apt_type = primary_apt.get("_geon_type", "campaign")

        # Build a timeline of relevant events.
        timeline: list[dict[str, str]] = [
            {
                "date": worst_event.get("date", now),
                "type": "diplomatic",
                "description": (
                    f"Goldstein {goldstein}: "
                    f"{worst_event.get('cameo_description', 'Diplomatic event')}"
                ),
            },
        ]
        for apt in apt_matches[:3]:  # Limit timeline entries.
            timeline.append({
                "date": apt.get("modified", apt.get("created", now)),
                "type": "cyber",
                "description": f"{apt.get('name', 'Unknown')} ({apt_type})",
            })

        # Situation-stable identity: one correlation per (sorted) country
        # pair. The previous hash used the unsorted pair AND the current
        # date, so (A,B)/(B,A) diverged and the same escalation re-alerted
        # daily; the engine now updates the existing document instead.
        pair = "||".join(sorted([country_a, country_b]))
        correlation_id = hashlib.sha256(
            f"{self.RULE_NAME}:{pair}".encode()
        ).hexdigest()[:20]

        # Evidence: the worst GDELT events + the APT entities, so the
        # analyst can verify the claim instead of trusting the alert.
        evidence: list[dict[str, str]] = []
        for hit in (event_hits or [])[:5]:
            src = hit.get("_source", {})
            evidence.append(evidence_from_hit(
                hit, "diplomatic",
                f"Goldstein {src.get('goldstein_scale')}: "
                f"{src.get('cameo_description', '')}",
            ))
        for apt in apt_matches[:3]:
            evidence.append(evidence_entry(
                index="opencti",
                doc_id=apt.get("id", ""),
                date=str(apt.get("modified", apt.get("created", ""))),
                kind="cyber",
                summary=f"{apt.get('name', 'Unknown')} "
                        f"({apt.get('_geon_type', 'campaign')})",
            ))

        # Confidence: STIX-validated attribution + corroboration volume
        # + statistical strength of the escalation.
        conf, factors = confidence(30, {
            "attribution": attribution_bonus("opencti"),
            "volume": volume_bonus(len(event_hits or [])),
            "zscore": zscore_bonus(
                baseline.zscore if baseline else None, ZSCORE_THRESHOLD),
        })

        doc: dict[str, Any] = {
            "correlation_id": correlation_id,
            "timestamp": now,
            "date": now,
            "rule_name": self.RULE_NAME,
            "severity": severity,
            "countries_involved": sorted([country_a, country_b]),
            "confidence": conf,
            "confidence_factors": factors,
            "evidence": evidence,
            "diplomatic_event": {
                "event_id": worst_event.get("event_id", ""),
                "description": worst_event.get("cameo_description", ""),
                "goldstein": goldstein,
            },
            "cyber_event": {
                "campaign_id": primary_apt.get("id", ""),
                "apt_group": apt_name,
                "techniques": [],  # Techniques require deeper relationship resolution.
            },
            "description": (
                f"Diplomatic escalation between {country_a} and {country_b} "
                f"(Goldstein {goldstein}) detected within {APT_WINDOW_DAYS} days "
                f"of APT activity by {apt_name}."
            ),
            "timeline": timeline,
        }
        if baseline is not None:
            doc["baseline"] = {
                "zscore": round(baseline.zscore, 2),
                "current_rate": baseline.current_rate,
                "baseline_mean": baseline.baseline_mean,
                "baseline_std": baseline.baseline_std,
                "baseline_days": baseline.baseline_days,
            }
            doc["description"] += (
                f" Pair activity at {baseline.zscore:.1f} standard deviations "
                f"above its {baseline.baseline_days}-day baseline."
            )
        return doc

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _pair_key(country_a: str, country_b: str) -> str:
        """Build an order-independent key for a country pair."""
        a, b = sorted([country_a, country_b])
        return f"{a}||{b}"

    @staticmethod
    def _compute_severity(
        goldstein: float, apt_matches: list[dict[str, Any]]
    ) -> str:
        """Derive alert severity from the Goldstein score and APT data.

        Args:
            goldstein: Goldstein scale value (negative = more severe).
            apt_matches: List of APT campaign/intrusion set dicts.

        Returns:
            One of ``"low"``, ``"medium"``, ``"high"``, ``"critical"``.
        """
        # Base severity from Goldstein score.
        if goldstein <= -8:
            base = 3  # critical
        elif goldstein <= -6:
            base = 2  # high
        else:
            base = 1  # medium

        # Boost if APT matches have high confidence.
        max_confidence = max(
            (m.get("confidence", 0) or 0 for m in apt_matches),
            default=0,
        )
        if max_confidence >= 80:
            base = min(base + 1, 3)

        return {0: "low", 1: "medium", 2: "high", 3: "critical"}.get(base, "medium")
