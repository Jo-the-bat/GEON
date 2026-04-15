"""GEON Prediction Consensus ingestor.

Fetches geopolitical prediction markets from Manifold Markets (public)
and Metaculus (requires token), matches them against Polymarket cases,
computes consensus scores, and detects divergences.

Usage::

    python -m prediction_consensus.ingestor
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from common.config import INDEX_PREFIX, setup_logging
from common.es_client import bulk_index, ensure_index, get_es_client
from prediction_consensus.matcher import compute_consensus, find_matches
from prediction_consensus.parser import (
    extract_countries,
    is_geopolitical,
    normalize_manifold_market,
    normalize_metaculus_question,
)

logger = logging.getLogger(__name__)

PREDICTIONS_INDEX = f"{INDEX_PREFIX}-predictions"
POLYMARKET_INDEX = f"{INDEX_PREFIX}-polymarket-cases"
CORRELATIONS_INDEX = f"{INDEX_PREFIX}-correlations"
MAPPING_PATH = Path(__file__).resolve().parent / "mapping.json"

MANIFOLD_API = "https://api.manifold.markets/v0/search-markets"
METACULUS_API = "https://www.metaculus.com/api/questions/"

METACULUS_TOKEN = os.getenv("METACULUS_API_TOKEN", "")
DIVERGENCE_ALERT_THRESHOLD = 0.15

GEO_SEARCH_TERMS = [
    "war", "ceasefire", "nuclear", "sanctions", "invasion",
    "military", "nato", "conflict", "geopolitics",
]


class PredictionConsensusIngestor:
    """Fetches prediction markets, computes consensus, detects divergences."""

    def __init__(self) -> None:
        self.es = get_es_client()

    # ------------------------------------------------------------------
    # Manifold Markets (public, no auth)
    # ------------------------------------------------------------------

    @retry(
        retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=5, max=30),
        reraise=True,
    )
    def _fetch_manifold(self) -> list[dict[str, Any]]:
        """Fetch geopolitical markets from Manifold Markets."""
        all_markets: list[dict[str, Any]] = []
        for term in GEO_SEARCH_TERMS:
            try:
                resp = requests.get(
                    MANIFOLD_API,
                    params={"term": term, "limit": 50, "sort": "liquidity"},
                    timeout=30,
                )
                resp.raise_for_status()
                markets = resp.json()
                if isinstance(markets, list):
                    all_markets.extend(markets)
            except Exception:
                logger.warning("Manifold search for '%s' failed.", term)

        # Deduplicate by id
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for m in all_markets:
            mid = m.get("id", "")
            if mid and mid not in seen:
                seen.add(mid)
                unique.append(m)

        logger.info("Fetched %d unique Manifold markets.", len(unique))
        return unique

    # ------------------------------------------------------------------
    # Metaculus (requires token)
    # ------------------------------------------------------------------

    def _fetch_metaculus(self) -> list[dict[str, Any]]:
        """Fetch geopolitical questions from Metaculus."""
        if not METACULUS_TOKEN:
            logger.info("METACULUS_API_TOKEN not set — skipping Metaculus.")
            return []

        all_questions: list[dict[str, Any]] = []
        headers = {"Authorization": f"Token {METACULUS_TOKEN}"}

        for term in GEO_SEARCH_TERMS[:5]:  # Limit to avoid rate limits
            try:
                resp = requests.get(
                    METACULUS_API,
                    params={
                        "search": term,
                        "format": "json",
                        "limit": 30,
                        "type": "forecast",
                        "status": "open",
                    },
                    headers=headers,
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()
                results = data.get("results", []) if isinstance(data, dict) else data
                if isinstance(results, list):
                    all_questions.extend(results)
            except Exception:
                logger.warning("Metaculus search for '%s' failed.", term)

        # Deduplicate
        seen: set[int] = set()
        unique: list[dict[str, Any]] = []
        for q in all_questions:
            qid = q.get("id", 0)
            if qid and qid not in seen:
                seen.add(qid)
                unique.append(q)

        logger.info("Fetched %d unique Metaculus questions.", len(unique))
        return unique

    # ------------------------------------------------------------------
    # Load existing Polymarket cases
    # ------------------------------------------------------------------

    def _load_polymarket_cases(self) -> list[dict[str, Any]]:
        """Load active Polymarket cases from ES."""
        try:
            resp = self.es.search(
                index=POLYMARKET_INDEX,
                query={"term": {"status": "active"}},
                size=500,
            )
            results = []
            for h in resp["hits"]["hits"]:
                doc = h["_source"]
                doc["_es_id"] = h["_id"]  # preserve ES _id for updates
                results.append(doc)
            return results
        except Exception:
            logger.warning("Could not load Polymarket cases.")
            return []

    # ------------------------------------------------------------------
    # Main pipeline
    # ------------------------------------------------------------------

    def ingest(self) -> int:
        """Fetch, normalize, match, enrich, and index."""
        ensure_index(self.es, PREDICTIONS_INDEX, MAPPING_PATH)

        # 1. Fetch external markets
        manifold_raw = self._fetch_manifold()
        metaculus_raw = self._fetch_metaculus()

        # 2. Normalize and filter for geopolitical content
        external_markets: list[dict[str, Any]] = []

        for m in manifold_raw:
            doc = normalize_manifold_market(m)
            if doc:
                external_markets.append(doc)

        for q in metaculus_raw:
            doc = normalize_metaculus_question(q)
            if doc:
                external_markets.append(doc)

        logger.info(
            "Normalized %d geopolitical markets (Manifold + Metaculus).",
            len(external_markets),
        )

        # 3. Index external markets into geon-predictions
        indexed = 0
        if external_markets:
            indexed = bulk_index(
                self.es, PREDICTIONS_INDEX, external_markets, id_field="market_id"
            )
            logger.info("Indexed %d markets into %s.", indexed, PREDICTIONS_INDEX)

        # 4. Match against Polymarket and compute consensus
        pm_cases = self._load_polymarket_cases()
        if pm_cases and external_markets:
            matches = find_matches(pm_cases, external_markets)
            self._enrich_polymarket(pm_cases, matches)

        return indexed

    def _enrich_polymarket(
        self,
        pm_cases: list[dict[str, Any]],
        matches: dict[str, list[dict[str, Any]]],
    ) -> None:
        """Enrich Polymarket cases with consensus data and detect divergences."""
        divergence_alerts: list[dict[str, Any]] = []

        for pm_case in pm_cases:
            # Polymarket uses case_id as the document _id
            pm_id = pm_case.get("case_id", pm_case.get("_es_id", ""))
            matched = matches.get(pm_id, [])
            if not matched:
                continue

            pm_price = pm_case.get("outcome_yes_price", 0.0)
            consensus = compute_consensus(pm_price, matched)

            # Update the Polymarket case in ES
            try:
                self.es.update(
                    index=POLYMARKET_INDEX,
                    id=pm_id,
                    body={"doc": {"consensus": consensus}},
                )
                logger.debug("Enriched Polymarket case %s with consensus.", pm_id)
            except Exception:
                logger.warning("Failed to update Polymarket case %s.", pm_id)

            # Check for divergence alert
            if consensus["divergence"] > DIVERGENCE_ALERT_THRESHOLD:
                divergence_alerts.append(
                    self._build_divergence_alert(pm_case, consensus)
                )

        if divergence_alerts:
            bulk_index(
                self.es, CORRELATIONS_INDEX, divergence_alerts, id_field="correlation_id"
            )
            logger.info(
                "Created %d divergence alert(s).", len(divergence_alerts)
            )

    def _build_divergence_alert(
        self,
        pm_case: dict[str, Any],
        consensus: dict[str, Any],
    ) -> dict[str, Any]:
        """Build a correlation document for a prediction divergence."""
        now = datetime.now(timezone.utc).isoformat()
        question = pm_case.get("question", "")
        countries = pm_case.get("countries_involved", [])
        divergence = consensus["divergence"]

        severity = "high" if divergence > 0.20 else "medium"

        correlation_id = hashlib.sha256(
            f"prediction_divergence:{pm_case.get('market_id', '')}:{now[:10]}".encode()
        ).hexdigest()[:20]

        return {
            "correlation_id": correlation_id,
            "timestamp": now,
            "date": now,
            "rule_name": "prediction_divergence",
            "severity": severity,
            "countries_involved": countries,
            "diplomatic_event": {
                "event_id": "",
                "description": question,
                "goldstein": 0.0,
            },
            "cyber_event": {
                "campaign_id": "",
                "apt_group": "",
                "techniques": [],
            },
            "description": (
                f"Prediction market divergence ({divergence:.2%}) detected for: "
                f"\"{question}\". Polymarket={consensus['polymarket_yes']:.1%}, "
                f"Metaculus={consensus.get('metaculus_median', 'N/A')}, "
                f"Manifold={consensus.get('manifold_yes', 'N/A')}. "
                f"High divergence indicates strong disagreement between platforms."
            ),
            "timeline": [],
        }


def main() -> None:
    setup_logging("prediction_consensus.ingestor")
    ing = PredictionConsensusIngestor()
    count = ing.ingest()
    logger.info("Done. %d external markets indexed.", count)


if __name__ == "__main__":
    main()
