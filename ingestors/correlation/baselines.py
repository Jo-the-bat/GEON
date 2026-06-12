"""Per-country / per-pair statistical baselines for correlation rules.

The original rules applied the same absolute thresholds to every country,
so chronically tense pairs (Russia-Ukraine) triggered permanently while
unusual deterioration in normally quiet countries stayed under the bar.
This module measures deviation from each subject's OWN history instead:

    z = (current_daily_rate - baseline_mean) / baseline_std

computed from a daily date-histogram of negative GDELT events over a
rolling baseline window (excluding the current detection window).

Rule 4 (rhetoric_shift) pioneered this approach for tone; rules 1 and 10
use these helpers for event volume. ``as_of`` makes the computation
replayable by the backtesting harness.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from common.config import INDEX_PREFIX
from common.settings import setting

logger = logging.getLogger(__name__)

GDELT_INDEX_PATTERN = f"{INDEX_PREFIX}-gdelt-events-*"

BASELINE_DAYS: int = setting("correlation.baselines.baseline_days", 90)
# Daily-rate std floor: prevents division blow-ups for very quiet
# subjects (a single event must not become a 10-sigma anomaly).
STD_FLOOR: float = setting("correlation.baselines.std_floor", 1.0)
# Minimum days of history required for the baseline to be meaningful.
MIN_BASELINE_DAYS: int = setting("correlation.baselines.min_baseline_days", 21)


@dataclass
class Baseline:
    """Result of a baseline comparison.

    Attributes:
        zscore: Standard deviations of the current rate above the
            baseline mean.
        current_rate: Mean daily event count over the detection window.
        baseline_mean: Mean daily event count over the baseline window.
        baseline_std: Std (floored) of the baseline daily counts.
        baseline_days: Number of days the baseline covers.
    """

    zscore: float
    current_rate: float
    baseline_mean: float
    baseline_std: float
    baseline_days: int


def _country_clause(country_a: str, country_b: str | None) -> dict[str, Any]:
    """Bool clause matching events involving the country (or the pair)."""
    if country_b:
        return {
            "bool": {
                "should": [
                    {"bool": {"filter": [
                        {"term": {"source_country": country_a}},
                        {"term": {"target_country": country_b}},
                    ]}},
                    {"bool": {"filter": [
                        {"term": {"source_country": country_b}},
                        {"term": {"target_country": country_a}},
                    ]}},
                ],
                "minimum_should_match": 1,
            }
        }
    return {
        "bool": {
            "should": [
                {"term": {"source_country": country_a}},
                {"term": {"target_country": country_a}},
            ],
            "minimum_should_match": 1,
        }
    }


def negative_events_baseline(
    es: Any,
    country_a: str,
    country_b: str | None = None,
    *,
    goldstein_lt: float = -3.0,
    window_days: int = 7,
    baseline_days: int | None = None,
    as_of: datetime | None = None,
) -> Baseline | None:
    """Compare the current negative-event rate to the subject's history.

    Args:
        es: Elasticsearch client.
        country_a: Country (canonical form).
        country_b: Optional second country — pair mode (both directions).
        goldstein_lt: Negative-event cutoff.
        window_days: Detection window (the recent period being judged).
        baseline_days: History length; defaults to the configured value.
        as_of: Clock override for backtesting (defaults to now).

    Returns:
        :class:`Baseline`, or ``None`` when the history is too short or
        the query fails (callers should then fall back to their absolute
        thresholds).
    """
    baseline_days = baseline_days or BASELINE_DAYS
    now = as_of or datetime.now(timezone.utc)
    # Work on COMPLETE UTC days only: the partial "today" bucket would
    # systematically deflate the current rate (e.g. minutes after
    # midnight a 7-day window would read 6 days + ~nothing), and a
    # bucket straddling the window boundary would leak the detection
    # window into its own baseline.
    today_floor = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    window_start = today_floor - timedelta(days=window_days)
    start = window_start - timedelta(days=baseline_days)

    try:
        resp = es.search(
            index=GDELT_INDEX_PATTERN,
            size=0,
            query={
                "bool": {
                    "filter": [
                        {"range": {"date": {
                            "gte": start.isoformat(),
                            "lte": now.isoformat(),
                        }}},
                        {"range": {"goldstein_scale": {"lt": goldstein_lt}}},
                        _country_clause(country_a, country_b),
                    ]
                }
            },
            aggs={
                "daily": {
                    "date_histogram": {
                        "field": "date",
                        "calendar_interval": "1d",
                        "min_doc_count": 0,
                        "extended_bounds": {
                            "min": start.isoformat(),
                            "max": now.isoformat(),
                        },
                    }
                }
            },
        )
    except Exception:
        logger.warning(
            "Baseline query failed for %s%s.",
            country_a, f"/{country_b}" if country_b else "",
            exc_info=True,
        )
        return None

    try:
        buckets = resp["aggregations"]["daily"]["buckets"]
    except (KeyError, TypeError):
        return None

    baseline_counts: list[int] = []
    window_counts: list[int] = []
    window_start_ms = window_start.timestamp() * 1000
    today_floor_ms = today_floor.timestamp() * 1000
    for bucket in buckets:
        count = int(bucket.get("doc_count", 0))
        key = bucket.get("key", 0)
        if key >= today_floor_ms:
            continue  # partial current day — excluded from both sides
        if key >= window_start_ms:
            window_counts.append(count)
        else:
            baseline_counts.append(count)

    # Drop leading zero-days before the first observed event: an index
    # that only covers 60 days must not count absent history as "quiet".
    # (Conservative bias: a genuinely calm pre-activity period is also
    # trimmed, which can only UNDER-detect, never over-alert.)
    first_nonzero = next(
        (i for i, c in enumerate(baseline_counts) if c > 0), None)
    baseline_counts = (
        baseline_counts[first_nonzero:] if first_nonzero is not None else [])

    if len(baseline_counts) < MIN_BASELINE_DAYS or not window_counts:
        return None

    mean = sum(baseline_counts) / len(baseline_counts)
    variance = sum((c - mean) ** 2 for c in baseline_counts) / len(baseline_counts)
    std = max(variance ** 0.5, STD_FLOOR)
    # Fixed denominator: complete window days, not bucket count.
    current = sum(window_counts) / max(window_days, 1)

    return Baseline(
        zscore=(current - mean) / std,
        current_rate=round(current, 2),
        baseline_mean=round(mean, 2),
        baseline_std=round(std, 2),
        baseline_days=len(baseline_counts),
    )
