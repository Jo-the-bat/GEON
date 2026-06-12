"""Correlation Rule 4: Rhetoric change detection.

Detects significant shifts in media tone (GDELT) for country pairs.  When
the current 7-day average tone deviates by more than 2 standard deviations
from the 30-day rolling average, a "weak signal" alert is generated.
"""

from __future__ import annotations

import hashlib
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from common.config import INDEX_PREFIX
from common.countries import CANONICAL_COUNTRIES
from common.settings import setting
from elasticsearch import Elasticsearch

from correlation.scoring import (
    confidence,
    evidence_entry,
    volume_bonus,
    zscore_bonus,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
SHORT_WINDOW_DAYS: int = setting("correlation.rhetoric_shift.short_window_days", 7)
LONG_WINDOW_DAYS: int = setting("correlation.rhetoric_shift.long_window_days", 30)
STDDEV_THRESHOLD: float = setting("correlation.rhetoric_shift.stddev_threshold", 2.0)
# Minimum articles in the short window for significance.
MIN_ARTICLES: int = setting("correlation.rhetoric_shift.min_articles", 10)
# Tone-std floor: syndicated copies of one article give a near-zero
# baseline variance and explosive z-scores.
STD_FLOOR: float = setting("correlation.rhetoric_shift.std_floor", 1.0)
GDELT_INDEX_PATTERN = f"{INDEX_PREFIX}-gdelt-events-*"


class RhetoricShiftRule:
    """Rule 4: Rhetoric change detection via GDELT tone analysis.

    Aggregates GDELT tone by country pair over two time windows:
    - A **short window** (7 days) representing the current situation.
    - A **long window** (30 days) representing the baseline.

    If the short-window average tone deviates by more than 2 standard
    deviations from the long-window distribution, the rule fires a
    "weak signal" alert.

    Attributes:
        es: Elasticsearch client.
    """

    RULE_NAME = "rhetoric_shift"

    def __init__(
        self,
        es: Elasticsearch,
        as_of: "datetime | None" = None,
        **_kwargs: Any,
    ) -> None:
        """Initialise the rule.

        Args:
            es: Elasticsearch client.
            as_of: Clock override for the backtesting harness.
            **_kwargs: Accepts and ignores extra keyword arguments so the
                engine can pass ``octi`` without error.
        """
        self.es = es
        self.as_of = as_of

    def _now(self) -> datetime:
        return self.as_of or datetime.now(timezone.utc)

    def run(self) -> list[dict[str, Any]]:
        """Execute the rule and return a list of correlation documents.

        Returns:
            List of correlation dicts ready for indexing.
        """
        correlations: list[dict[str, Any]] = []

        # Step 1: Get country pairs with enough activity in the short window.
        short_stats = self._aggregate_tone(days=SHORT_WINDOW_DAYS)
        if not short_stats:
            logger.info("[%s] No GDELT tone data in the short window.", self.RULE_NAME)
            return correlations

        # Step 2: Get baseline statistics — the long window EXCLUDES the
        # short window so the shift under test does not contaminate its
        # own baseline (it would otherwise drag the mean toward itself
        # and understate the deviation by ~SHORT/LONG).
        long_stats = self._aggregate_tone(
            days=LONG_WINDOW_DAYS, exclude_recent_days=SHORT_WINDOW_DAYS)

        # Step 3: Compare and detect deviations.
        for pair_key, short in short_stats.items():
            if short["count"] < MIN_ARTICLES:
                continue  # Not enough data to be statistically meaningful.

            baseline = long_stats.get(pair_key)
            if not baseline or baseline["count"] < MIN_ARTICLES:
                continue

            deviation = self._compute_deviation(
                short["avg_tone"], baseline["avg_tone"], baseline["std_tone"]
            )

            if deviation is not None and abs(deviation) >= STDDEV_THRESHOLD:
                correlation = self._build_correlation(
                    pair_key, short, baseline, deviation
                )
                correlations.append(correlation)

        logger.info(
            "[%s] Generated %d rhetoric-shift signal(s).",
            self.RULE_NAME,
            len(correlations),
        )
        return correlations

    # ------------------------------------------------------------------
    # Tone aggregation
    # ------------------------------------------------------------------

    def _aggregate_tone(
        self, days: int, exclude_recent_days: int = 0
    ) -> dict[str, dict[str, Any]]:
        """Aggregate GDELT tone by country pair over the given window.

        Uses a composite aggregation keyed on ``(source_country,
        target_country)`` with stats sub-aggregation on the ``tone`` field.

        Args:
            days: Window length in days.
            exclude_recent_days: Shift the window back by this many days
                (used so the baseline window ends BEFORE the detection
                window instead of containing it).

        Returns:
            Dict mapping ``"countryA||countryB"`` to a dict with keys
            ``avg_tone``, ``std_tone``, ``count``, ``min_tone``,
            ``max_tone``.
        """
        until = self._now() - timedelta(days=exclude_recent_days)
        since = (until - timedelta(days=days)).isoformat()

        query: dict[str, Any] = {
            "bool": {
                "must": [
                    # The upper bound matters for the backtesting harness
                    # (a replay must not see future events) AND for the
                    # baseline call (which ends before the short window).
                    {"range": {"date": {
                        "gte": since,
                        "lte": until.isoformat(),
                    }}},
                    {"exists": {"field": "tone"}},
                ],
                "must_not": [
                    {"term": {"source_country": ""}},
                    {"term": {"target_country": ""}},
                ],
            }
        }

        aggs: dict[str, Any] = {
            "country_pairs": {
                "composite": {
                    "size": 500,
                    "sources": [
                        {"src": {"terms": {"field": "source_country"}}},
                        {"tgt": {"terms": {"field": "target_country"}}},
                    ],
                },
                "aggs": {
                    "tone_stats": {
                        "extended_stats": {"field": "tone"},
                    },
                },
            },
        }

        results: dict[str, dict[str, Any]] = {}
        after_key = None

        # Paginate through all composite buckets.
        for _ in range(20):  # Safety limit on pagination rounds.
            if after_key:
                aggs["country_pairs"]["composite"]["after"] = after_key

            try:
                resp = self.es.search(
                    index=GDELT_INDEX_PATTERN,
                    query=query,
                    aggs=aggs,
                    size=0,
                )
            except Exception:
                logger.exception(
                    "[%s] Failed to aggregate GDELT tone (window=%d days).",
                    self.RULE_NAME,
                    days,
                )
                break

            buckets = (
                resp.get("aggregations", {})
                .get("country_pairs", {})
                .get("buckets", [])
            )
            if not buckets:
                break

            for bucket in buckets:
                src = bucket["key"]["src"]
                tgt = bucket["key"]["tgt"]
                # GDELT actor pseudo-codes (WST, EUR, GOV...) survive in
                # the indexed country fields — a "rhetoric shift" toward
                # a region code is noise, not intelligence.
                if (src not in CANONICAL_COUNTRIES
                        or tgt not in CANONICAL_COUNTRIES):
                    continue
                pair_key = self._pair_key(src, tgt)

                stats = bucket.get("tone_stats", {})
                count = stats.get("count", 0)
                if count == 0:
                    continue

                avg_tone = stats.get("avg", 0.0)
                std_dev = stats.get("std_deviation", 0.0)

                # Merge if we already have this pair (composite may return
                # A->B and B->A separately).
                existing = results.get(pair_key)
                if existing:
                    # Weighted merge of stats.
                    total = existing["count"] + count
                    existing["avg_tone"] = (
                        existing["avg_tone"] * existing["count"]
                        + avg_tone * count
                    ) / total
                    existing["count"] = total
                    existing["min_tone"] = min(
                        existing["min_tone"], stats.get("min", 0.0)
                    )
                    existing["max_tone"] = max(
                        existing["max_tone"], stats.get("max", 0.0)
                    )
                    # Approximate merged std deviation.
                    existing["std_tone"] = max(existing["std_tone"], std_dev)
                else:
                    results[pair_key] = {
                        "avg_tone": avg_tone,
                        "std_tone": std_dev,
                        "count": count,
                        "min_tone": stats.get("min", 0.0),
                        "max_tone": stats.get("max", 0.0),
                        "source_country": src,
                        "target_country": tgt,
                    }

            after_key = (
                resp.get("aggregations", {})
                .get("country_pairs", {})
                .get("after_key")
            )
            if not after_key:
                break

        logger.debug(
            "[%s] Aggregated tone for %d country pairs (window=%d days).",
            self.RULE_NAME,
            len(results),
            days,
        )
        return results

    # ------------------------------------------------------------------
    # Deviation calculation
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_deviation(
        current_avg: float,
        baseline_avg: float,
        baseline_std: float,
    ) -> float | None:
        """Compute how many standard deviations the current average deviates.

        The std is floored: low-coverage pairs fed by syndicated copies
        of one article have a near-zero tone variance, and dividing by
        it produced absurd deviations (z > 26000 observed in the first
        production backtest — the harness paying for itself).

        Args:
            current_avg: Short-window average tone.
            baseline_avg: Long-window average tone.
            baseline_std: Long-window standard deviation of tone.

        Returns:
            Number of standard deviations (signed), or ``None`` if the
            baseline std is NaN.
        """
        if math.isnan(baseline_std):
            return None
        return (current_avg - baseline_avg) / max(baseline_std, STD_FLOOR)

    # ------------------------------------------------------------------
    # Build correlation
    # ------------------------------------------------------------------

    def _build_correlation(
        self,
        pair_key: str,
        short: dict[str, Any],
        baseline: dict[str, Any],
        deviation: float,
    ) -> dict[str, Any]:
        """Assemble a rhetoric-shift correlation document.

        Args:
            pair_key: Country pair key (``"A||B"``).
            short: Short-window aggregation stats.
            baseline: Long-window aggregation stats.
            deviation: Number of standard deviations.

        Returns:
            Correlation document dict.
        """
        now = self._now().isoformat()
        countries = pair_key.split("||")

        # Negative deviation = rhetoric deteriorating; positive = improving.
        direction = "deteriorating" if deviation < 0 else "improving"
        abs_dev = abs(deviation)

        # Severity for rhetoric shifts is generally lower (weak signals).
        if abs_dev >= 4.0:
            severity = "high"
        elif abs_dev >= 3.0:
            severity = "medium"
        else:
            severity = "low"

        # Situation-stable identity: one correlation per country pair; the
        # engine tracks persistence via last_seen/times_seen instead of
        # producing a new document every day.
        correlation_id = hashlib.sha256(
            f"{self.RULE_NAME}:{pair_key}".encode()
        ).hexdigest()[:20]

        # Pure-aggregation rule: the evidence is the statistical signal
        # itself (there are no individual trigger documents to point at).
        evidence = [evidence_entry(
            index=GDELT_INDEX_PATTERN,
            doc_id="",
            date=now,
            kind="signal",
            summary=(
                f"Tone {direction}: {SHORT_WINDOW_DAYS}d avg "
                f"{short['avg_tone']:.2f} vs {LONG_WINDOW_DAYS}d baseline "
                f"{baseline['avg_tone']:.2f} ({deviation:+.1f} sigma, "
                f"n={short['count']}/{baseline['count']})"
            ),
        )]
        conf, factors = confidence(25, {
            "zscore": zscore_bonus(abs_dev, STDDEV_THRESHOLD),
            "volume": volume_bonus(int(short.get("count", 0)), saturation=100),
        })

        return {
            "correlation_id": correlation_id,
            "timestamp": now,
            "date": now,
            "rule_name": self.RULE_NAME,
            "severity": severity,
            "countries_involved": countries,
            "deviation": round(deviation, 2),
            "confidence": conf,
            "confidence_factors": factors,
            "evidence": evidence,
            "diplomatic_event": {
                "event_id": "",
                "description": (
                    f"Rhetoric shift ({direction}): "
                    f"{SHORT_WINDOW_DAYS}-day avg tone = {short['avg_tone']:.2f} "
                    f"vs {LONG_WINDOW_DAYS}-day baseline = {baseline['avg_tone']:.2f} "
                    f"(deviation: {deviation:+.1f} sigma)"
                ),
                "goldstein": 0.0,
            },
            "cyber_event": {
                "campaign_id": "",
                "apt_group": "",
                "techniques": [],
            },
            "description": (
                f"Significant rhetoric shift detected between "
                f"{countries[0]} and {countries[1]}: media tone is "
                f"{direction} ({deviation:+.1f} standard deviations from "
                f"the {LONG_WINDOW_DAYS}-day baseline). "
                f"Current {SHORT_WINDOW_DAYS}-day avg tone: {short['avg_tone']:.2f}, "
                f"baseline avg: {baseline['avg_tone']:.2f}. "
                f"This is a weak signal that warrants monitoring."
            ),
            "timeline": [
                {
                    "date": (
                        self._now() - timedelta(days=LONG_WINDOW_DAYS)
                    ).isoformat(),
                    "type": "baseline",
                    "description": (
                        f"{LONG_WINDOW_DAYS}-day baseline avg tone: "
                        f"{baseline['avg_tone']:.2f} "
                        f"(std: {baseline['std_tone']:.2f}, "
                        f"n={baseline['count']})"
                    ),
                },
                {
                    "date": (
                        self._now() - timedelta(days=SHORT_WINDOW_DAYS)
                    ).isoformat(),
                    "type": "shift",
                    "description": (
                        f"{SHORT_WINDOW_DAYS}-day avg tone: "
                        f"{short['avg_tone']:.2f} "
                        f"({deviation:+.1f} sigma, n={short['count']})"
                    ),
                },
            ],
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _pair_key(country_a: str, country_b: str) -> str:
        """Build an order-independent key for a country pair."""
        a, b = sorted([country_a, country_b])
        return f"{a}||{b}"
