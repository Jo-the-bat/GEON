"""GEON Polymarket geopolitical prediction markets ingestor.

Fetches markets from the Polymarket Gamma API, filters for geopolitical
content, enriches with GEON data (GDELT events, correlations, APT groups,
sanctions), detects significant price movements, and indexes into
Elasticsearch.

Usage::

    python -m polymarket.ingestor              # ingest + enrich
    python -m polymarket.ingestor --ingest     # ingest only
    python -m polymarket.ingestor --enrich     # enrich only
"""

from __future__ import annotations

import argparse
import json
import logging
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
from common.es_client import ensure_index, get_es_client
from polymarket.parser import extract_countries, is_geopolitical, normalize_market

logger = logging.getLogger(__name__)

INDEX_NAME = f"{INDEX_PREFIX}-polymarket-cases"
MAPPING_PATH = Path(__file__).resolve().parent / "mapping.json"
GAMMA_API_URL = "https://gamma-api.polymarket.com/markets"

# Load country-APT mapping for enrichment.
_APT_PATH = Path(__file__).resolve().parent.parent / "common" / "country_apt_mapping.json"
_COUNTRY_APT: dict[str, list[str]] = {}
try:
    with _APT_PATH.open() as f:
        _raw = json.load(f)
    _COUNTRY_APT = {k: v for k, v in _raw.items() if k != "_comment"}
except Exception:
    pass

PRICE_SHIFT_THRESHOLD = 0.10  # 10% shift triggers alert


class PolymarketIngestor:
    """Fetches, filters, enriches, and indexes Polymarket geopolitical markets."""

    def __init__(self) -> None:
        self.es = get_es_client()
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    # ------------------------------------------------------------------
    # Fetch markets from Gamma API
    # ------------------------------------------------------------------

    @retry(
        retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=5, max=30),
        reraise=True,
    )
    def _fetch_markets(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        """Fetch markets from the Polymarket Gamma API."""
        params: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "active": True,
            "closed": False,
        }
        resp = requests.get(GAMMA_API_URL, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def fetch_all_markets(self) -> list[dict[str, Any]]:
        """Fetch all active markets, paginated."""
        all_markets: list[dict[str, Any]] = []
        offset = 0
        limit = 100
        while True:
            batch = self._fetch_markets(limit=limit, offset=offset)
            if not batch:
                break
            all_markets.extend(batch)
            if len(batch) < limit:
                break
            offset += limit
        self.logger.info("Fetched %d markets from Polymarket.", len(all_markets))
        return all_markets

    # ------------------------------------------------------------------
    # Ingest: filter + normalize + index
    # ------------------------------------------------------------------

    def ingest(self) -> int:
        """Fetch, filter, and index geopolitical markets."""
        ensure_index(self.es, INDEX_NAME, MAPPING_PATH)

        markets = self.fetch_all_markets()
        geo_markets = [m for m in markets if is_geopolitical(m)]
        self.logger.info("Filtered to %d geopolitical markets.", len(geo_markets))

        if not geo_markets:
            return 0

        docs = [normalize_market(m) for m in geo_markets]

        # Detect price shifts by comparing with existing data.
        self._detect_price_shifts(docs)

        # Bulk index with case_id as _id for upsert.
        from elasticsearch import helpers
        actions = [{"_index": INDEX_NAME, "_id": d["case_id"], "_source": d} for d in docs]
        success, errors = helpers.bulk(self.es, actions, raise_on_error=False, stats_only=False)
        if errors:
            self.logger.error("Polymarket indexing: %d errors.", len(errors))
        self.logger.info("Indexed %d Polymarket cases.", success)
        return success

    # ------------------------------------------------------------------
    # Enrich: add GEON context to existing cases
    # ------------------------------------------------------------------

    def enrich(self) -> int:
        """Enrich active cases with GEON data (GDELT, correlations, APTs, sanctions)."""
        try:
            result = self.es.search(
                index=INDEX_NAME,
                body={"query": {"term": {"status": "active"}}, "size": 200},
            )
        except Exception:
            self.logger.warning("No Polymarket cases to enrich.")
            return 0

        hits = result["hits"]["hits"]
        if not hits:
            return 0

        updated = 0
        for hit in hits:
            doc = hit["_source"]
            countries = doc.get("countries_involved", [])
            if not countries:
                continue

            # Count GDELT events for involved countries (7 days).
            gdelt_count = self._count_gdelt(countries)
            corr_count = self._count_correlations(countries)
            sanctions_count = self._count_sanctions(countries)
            apt_groups = self._get_apt_groups(countries)

            update_body = {
                "related_gdelt_events": gdelt_count,
                "related_correlations": corr_count,
                "related_sanctions": sanctions_count,
                "related_apt_groups": apt_groups,
                "date": datetime.now(tz=timezone.utc).isoformat(),
            }

            self.es.update(
                index=INDEX_NAME,
                id=hit["_id"],
                body={"doc": update_body},
            )
            updated += 1

        self.logger.info("Enriched %d Polymarket cases.", updated)
        return updated

    # ------------------------------------------------------------------
    # Price shift detection
    # ------------------------------------------------------------------

    def _detect_price_shifts(self, new_docs: list[dict[str, Any]]) -> None:
        """Compare new prices with existing ones and flag significant shifts."""
        for doc in new_docs:
            try:
                existing = self.es.get(index=INDEX_NAME, id=doc["case_id"])
                old_yes = existing["_source"].get("outcome_yes_price", 0)
                new_yes = doc["outcome_yes_price"]
                change = abs(new_yes - old_yes)
                doc["price_change_24h"] = round(new_yes - old_yes, 4)

                if new_yes > old_yes:
                    doc["trend"] = "rising"
                elif new_yes < old_yes:
                    doc["trend"] = "falling"
                else:
                    doc["trend"] = "stable"

                if change >= PRICE_SHIFT_THRESHOLD:
                    self._create_shift_correlation(doc, old_yes, new_yes)
            except Exception:
                pass  # New case, no prior data.

    def _create_shift_correlation(
        self, doc: dict[str, Any], old_price: float, new_price: float,
    ) -> None:
        """Create a correlation entry for a significant price shift."""
        import hashlib
        now = datetime.now(tz=timezone.utc).isoformat()
        corr_id = hashlib.sha256(
            f"shift-{doc['case_id']}-{now}".encode()
        ).hexdigest()[:20]

        direction = "up" if new_price > old_price else "down"
        change_pct = round(abs(new_price - old_price) * 100, 1)

        correlation = {
            "correlation_id": corr_id,
            "timestamp": now,
            "date": now,
            "rule_name": "prediction_market_shift",
            "severity": "high" if change_pct >= 20 else "medium",
            "countries_involved": doc.get("countries_involved", []),
            "description": (
                f"Prediction market shift: '{doc['question']}' moved "
                f"{direction} by {change_pct}% (YES: {old_price:.0%} → {new_price:.0%})"
            ),
            "diplomatic_event": {},
            "cyber_event": {},
            "timeline": [],
        }

        try:
            self.es.index(
                index=f"{INDEX_PREFIX}-correlations",
                id=corr_id,
                body=correlation,
            )
            self.logger.info("Created price shift correlation: %s (%.1f%%)", corr_id, change_pct)
        except Exception:
            self.logger.warning("Failed to create shift correlation.", exc_info=True)

    # ------------------------------------------------------------------
    # GEON enrichment queries
    # ------------------------------------------------------------------

    def _count_gdelt(self, countries: list[str]) -> int:
        try:
            result = self.es.count(
                index=f"{INDEX_PREFIX}-gdelt-*",
                body={
                    "query": {"bool": {"filter": [
                        {"range": {"date": {"gte": "now-7d"}}},
                        {"terms": {"source_country": countries}},
                    ]}}
                },
            )
            return result["count"]
        except Exception:
            return 0

    def _count_correlations(self, countries: list[str]) -> int:
        try:
            result = self.es.count(
                index=f"{INDEX_PREFIX}-correlations",
                body={
                    "query": {"bool": {"filter": [
                        {"range": {"date": {"gte": "now-30d"}}},
                        {"terms": {"countries_involved": countries}},
                    ]}}
                },
            )
            return result["count"]
        except Exception:
            return 0

    def _count_sanctions(self, countries: list[str]) -> int:
        total = 0
        for c in countries:
            try:
                result = self.es.count(
                    index=f"{INDEX_PREFIX}-sanctions",
                    body={"query": {"term": {"country": c}}},
                )
                total += result["count"]
            except Exception:
                pass
        return total

    def _get_apt_groups(self, countries: list[str]) -> list[str]:
        groups: list[str] = []
        for c in countries:
            groups.extend(_COUNTRY_APT.get(c, []))
        return sorted(set(groups))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    setup_logging(level="INFO")
    ap = argparse.ArgumentParser()
    ap.add_argument("--ingest", action="store_true", help="Ingest only")
    ap.add_argument("--enrich", action="store_true", help="Enrich only")
    args = ap.parse_args()

    ing = PolymarketIngestor()
    if args.ingest:
        ing.ingest()
    elif args.enrich:
        ing.enrich()
    else:
        ing.ingest()
        ing.enrich()


if __name__ == "__main__":
    main()
