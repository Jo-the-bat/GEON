"""GEON country risk score calculator.

Computes a composite risk score (0-100) per country by aggregating seven
weighted components (v2, matches the current code below):

  - GDELT negative events (Goldstein < 0, 30d) — 25%
  - ACLED armed conflicts (30d)               — 15%
  - Active sanctions                          — 10%
  - Attributed APT groups                     — 15%
  - Detected correlations (30d)               — 20%
  - Military spending YoY increase            — 10%
  - Arms imports (TIV)                        — 5%

Indexed into ``geon-risk-scores``. One document per ``(country, day)``: the
``_id`` is ``"{country}:{YYYY-MM-DD}"`` so re-running the calculator on the
same day overwrites that day's score, but historical days are preserved and
dashboards can query a proper time series of risk evolution.

Usage::

    python -m risk_score.calculator
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common.config import INDEX_PREFIX, setup_logging
from common.es_client import ensure_index, get_es_client

logger = logging.getLogger(__name__)

INDEX_NAME = f"{INDEX_PREFIX}-risk-scores"
MAPPING_PATH = Path(__file__).resolve().parent / "mapping.json"

# Load country-APT mapping.
_APT_MAPPING_PATH = Path(__file__).resolve().parent.parent / "common" / "country_apt_mapping.json"
_COUNTRY_APT_MAP: dict[str, list[str]] = {}
try:
    with _APT_MAPPING_PATH.open() as f:
        _raw = json.load(f)
    _COUNTRY_APT_MAP = {k: v for k, v in _raw.items() if k != "_comment"}
except Exception:
    pass

# Top countries to score (covers major geopolitical actors).
TARGET_COUNTRIES: list[str] = [
    "UNITED STATES", "RUSSIA", "CHINA", "IRAN", "ISRAEL",
    "UKRAINE", "NORTH KOREA", "INDIA", "PAKISTAN", "TURKEY",
    "SAUDI ARABIA", "UNITED KINGDOM", "FRANCE", "GERMANY",
    "JAPAN", "SOUTH KOREA", "AUSTRALIA", "CANADA", "BRAZIL",
    "NIGERIA", "SOUTH AFRICA", "EGYPT", "IRAQ", "SYRIA",
    "LEBANON", "YEMEN", "AFGHANISTAN", "MYANMAR", "VENEZUELA",
    "MEXICO", "COLOMBIA", "ETHIOPIA", "SOMALIA", "SUDAN",
    "LIBYA", "TAIWAN", "PHILIPPINES", "INDONESIA", "THAILAND",
    "POLAND", "ROMANIA", "ITALY", "SPAIN", "NETHERLANDS",
]


class RiskScoreCalculator:
    """Calculates and indexes composite risk scores per country.

    One document per (country, day). Dashboards can now query time series of
    risk evolution (see the "Military Spending Trend" panel on the country
    profile dashboard for an example).
    """

    def __init__(self) -> None:
        self.es = get_es_client()

    def _count_gdelt_negative(self, country: str) -> int:
        """Count GDELT events with Goldstein < 0 for a country in last 30 days."""
        try:
            result = self.es.count(
                index=f"{INDEX_PREFIX}-gdelt-*",
                body={
                    "query": {
                        "bool": {
                            "filter": [
                                {"range": {"date": {"gte": "now-30d"}}},
                                {"range": {"goldstein_scale": {"lt": 0}}},
                                {"bool": {"should": [
                                    {"term": {"source_country": country}},
                                    {"term": {"target_country": country}},
                                ], "minimum_should_match": 1}},
                            ]
                        }
                    }
                },
            )
            return result["count"]
        except Exception:
            return 0

    def _count_acled(self, country: str) -> int:
        """Count ACLED conflict events for a country in last 30 days."""
        try:
            result = self.es.count(
                index=f"{INDEX_PREFIX}-acled-*",
                body={
                    "query": {
                        "bool": {
                            "filter": [
                                {"range": {"event_date": {"gte": "now-30d"}}},
                                {"term": {"country": country}},
                            ]
                        }
                    }
                },
            )
            return result["count"]
        except Exception:
            return 0

    def _count_sanctions(self, country: str) -> int:
        """Count active sanctions entries for a country."""
        try:
            result = self.es.count(
                index=f"{INDEX_PREFIX}-sanctions",
                body={
                    "query": {"term": {"country": country}}
                },
            )
            return result["count"]
        except Exception:
            return 0

    def _count_apt_groups(self, country: str) -> int:
        """Count known APT groups attributed to a country."""
        return len(_COUNTRY_APT_MAP.get(country, []))

    def _get_military_spending_yoy(self, country: str) -> float:
        """Get latest YoY military spending change for a country."""
        try:
            result = self.es.search(
                index=f"{INDEX_PREFIX}-military-spending",
                query={"term": {"country": country}},
                size=1,
                sort=[{"year": "desc"}],
            )
            hits = result.get("hits", {}).get("hits", [])
            if hits:
                return hits[0]["_source"].get("spending_change_yoy_pct", 0)
        except Exception:
            pass
        return 0

    def _count_arms_imports(self, country: str) -> float:
        """Sum TIV value of recent arms imports for a country."""
        try:
            result = self.es.search(
                index=f"{INDEX_PREFIX}-arms-transfers",
                query={"term": {"recipient_country": country}},
                aggs={"total_tiv": {"sum": {"field": "tiv_value"}}},
                size=0,
            )
            return result["aggregations"]["total_tiv"]["value"] or 0
        except Exception:
            return 0

    def _count_correlations(self, country: str) -> int:
        """Count correlations involving a country in last 30 days."""
        try:
            result = self.es.count(
                index=f"{INDEX_PREFIX}-correlations",
                body={
                    "query": {
                        "bool": {
                            "filter": [
                                {"range": {"date": {"gte": "now-30d"}}},
                                {"term": {"countries_involved": country}},
                            ]
                        }
                    }
                },
            )
            return result["count"]
        except Exception:
            return 0

    @staticmethod
    def _normalize(value: int, thresholds: tuple[int, int, int]) -> float:
        """Normalize a count to 0-100 scale using thresholds (low, mid, high)."""
        low, mid, high = thresholds
        if value <= 0:
            return 0.0
        if value >= high:
            return 100.0
        if value >= mid:
            return 50.0 + 50.0 * (value - mid) / (high - mid)
        return 50.0 * value / mid

    def calculate(self, country: str) -> dict[str, Any]:
        """Calculate risk score for a single country.

        Weights (v2, with SIPRI factors):
          - GDELT negative events — 25%
          - ACLED conflicts — 15%
          - Sanctions — 10%
          - APT groups — 15%
          - Correlations — 20%
          - Military spending increase — 10%
          - Arms imports — 5%
        """
        gdelt_neg = self._count_gdelt_negative(country)
        acled = self._count_acled(country)
        sanctions = self._count_sanctions(country)
        apt_count = self._count_apt_groups(country)
        correlations = self._count_correlations(country)
        milex_yoy = self._get_military_spending_yoy(country)
        arms_tiv = self._count_arms_imports(country)

        # Normalize each component to 0-100.
        gdelt_score = self._normalize(gdelt_neg, (100, 1000, 5000))
        acled_score = self._normalize(acled, (10, 100, 500))
        sanctions_score = self._normalize(sanctions, (5, 50, 200))
        apt_score = self._normalize(apt_count, (1, 3, 10))
        corr_score = self._normalize(correlations, (1, 5, 20))
        milex_score = self._normalize(int(max(milex_yoy, 0)), (5, 15, 30))
        arms_score = self._normalize(int(arms_tiv), (500, 3000, 10000))

        # Weighted composite (v2).
        risk_score = (
            gdelt_score * 0.25
            + acled_score * 0.15
            + sanctions_score * 0.10
            + apt_score * 0.15
            + corr_score * 0.20
            + milex_score * 0.10
            + arms_score * 0.05
        )
        risk_score = min(100.0, max(0.0, round(risk_score, 1)))

        if risk_score >= 75:
            risk_level = "critical"
        elif risk_score >= 50:
            risk_level = "high"
        elif risk_score >= 25:
            risk_level = "medium"
        else:
            risk_level = "low"

        now = datetime.now(tz=timezone.utc).isoformat()
        return {
            "country": country,
            "date": now,
            "risk_score": risk_score,
            "gdelt_negative_events": gdelt_neg,
            "gdelt_score": round(gdelt_score, 1),
            "acled_conflicts": acled,
            "acled_score": round(acled_score, 1),
            "sanctions_count": sanctions,
            "sanctions_score": round(sanctions_score, 1),
            "apt_groups_count": apt_count,
            "apt_score": round(apt_score, 1),
            "correlations_count": correlations,
            "correlations_score": round(corr_score, 1),
            "milex_yoy_pct": round(milex_yoy, 1),
            "milex_score": round(milex_score, 1),
            "arms_imports_tiv": round(arms_tiv, 0),
            "arms_score": round(arms_score, 1),
            "risk_level": risk_level,
        }

    def run(self) -> int:
        """Calculate risk scores for all target countries and index them."""
        ensure_index(self.es, INDEX_NAME, MAPPING_PATH)

        docs: list[dict[str, Any]] = []
        for country in TARGET_COUNTRIES:
            doc = self.calculate(country)
            docs.append(doc)
            logger.debug("%s: risk_score=%.1f (%s)", country, doc["risk_score"], doc["risk_level"])

        if not docs:
            return 0

        # Historise: one document per (country, day). Re-runs on the same day
        # overwrite, but historical days are preserved for trend dashboards.
        actions = []
        for doc in docs:
            actions.append({
                "_index": INDEX_NAME,
                "_id": f"{doc['country']}:{doc['date'][:10]}",
                "_source": doc,
            })

        from elasticsearch import helpers
        success, errors = helpers.bulk(self.es, actions, raise_on_error=False, stats_only=False)
        if errors:
            logger.error("Risk score indexing: %d errors.", len(errors))
        logger.info("Risk scores calculated: %d countries indexed.", success)
        return success


def main() -> None:
    setup_logging(level="INFO")
    calc = RiskScoreCalculator()
    calc.run()


if __name__ == "__main__":
    main()
