"""Tests for correlation Rule 4 (rhetoric shift via GDELT tone analysis).

Pure logic only — Elasticsearch is mocked with the exact composite
aggregation shape the rule reads in ``_aggregate_tone``.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingestors"))

import pytest
from correlation.rules.rhetoric_shift import (
    LONG_WINDOW_DAYS,
    MIN_ARTICLES,
    SHORT_WINDOW_DAYS,
    STDDEV_THRESHOLD,
    RhetoricShiftRule,
)

BASELINE_AVG = -2.0
BASELINE_STD = 1.5


def _rule() -> RhetoricShiftRule:
    return RhetoricShiftRule(es=MagicMock())


def _bucket(src, tgt, count, avg, std=BASELINE_STD, mn=-9.0, mx=3.0):
    """One composite-aggregation bucket as Elasticsearch returns it."""
    return {
        "key": {"src": src, "tgt": tgt},
        "tone_stats": {
            "count": count,
            "avg": avg,
            "std_deviation": std,
            "min": mn,
            "max": mx,
        },
    }


def _agg_resp(buckets, after_key=None):
    country_pairs = {"buckets": buckets}
    if after_key is not None:
        country_pairs["after_key"] = after_key
    return {"aggregations": {"country_pairs": country_pairs}}


def _short_avg(sigmas: float) -> float:
    """Short-window avg tone sitting *sigmas* std-devs below the baseline."""
    return BASELINE_AVG - sigmas * BASELINE_STD


class TestRunHappyPath:
    def test_deviation_above_threshold_fires(self):
        rule = _rule()
        short = _agg_resp(
            [_bucket("CHINA", "TAIWAN", MIN_ARTICLES + 40, _short_avg(STDDEV_THRESHOLD + 0.5))]
        )
        baseline = _agg_resp([_bucket("CHINA", "TAIWAN", MIN_ARTICLES + 290, BASELINE_AVG)])
        rule.es.search.side_effect = [short, baseline]

        out = rule.run()

        assert len(out) == 1
        corr = out[0]
        assert corr["rule_name"] == "rhetoric_shift"
        assert corr["severity"] in {"low", "medium", "high"}
        assert sorted(corr["countries_involved"]) == ["CHINA", "TAIWAN"]
        assert corr["correlation_id"]
        assert "deteriorating" in corr["description"]
        assert [e["type"] for e in corr["timeline"]] == ["baseline", "shift"]

    def test_exactly_at_threshold_fires(self):
        """The comparison is ``>=`` — a deviation of exactly the configured
        sigma threshold must fire."""
        rule = _rule()
        short = _agg_resp(
            [_bucket("CHINA", "TAIWAN", MIN_ARTICLES, _short_avg(STDDEV_THRESHOLD))]
        )
        baseline = _agg_resp([_bucket("CHINA", "TAIWAN", MIN_ARTICLES, BASELINE_AVG)])
        rule.es.search.side_effect = [short, baseline]
        assert len(rule.run()) == 1


class TestRunThresholdEdges:
    def test_deviation_just_below_threshold_no_fire(self):
        rule = _rule()
        short = _agg_resp(
            [_bucket("CHINA", "TAIWAN", MIN_ARTICLES + 40, _short_avg(STDDEV_THRESHOLD - 0.1))]
        )
        baseline = _agg_resp([_bucket("CHINA", "TAIWAN", MIN_ARTICLES + 290, BASELINE_AVG)])
        rule.es.search.side_effect = [short, baseline]
        assert rule.run() == []

    def test_short_window_below_min_articles_no_fire(self):
        """A huge deviation on too small a sample is not significant."""
        rule = _rule()
        short = _agg_resp(
            [_bucket("CHINA", "TAIWAN", MIN_ARTICLES - 1, _short_avg(STDDEV_THRESHOLD + 3.0))]
        )
        baseline = _agg_resp([_bucket("CHINA", "TAIWAN", MIN_ARTICLES + 290, BASELINE_AVG)])
        rule.es.search.side_effect = [short, baseline]
        assert rule.run() == []

    def test_baseline_below_min_articles_no_fire(self):
        rule = _rule()
        short = _agg_resp(
            [_bucket("CHINA", "TAIWAN", MIN_ARTICLES + 40, _short_avg(STDDEV_THRESHOLD + 1.0))]
        )
        baseline = _agg_resp([_bucket("CHINA", "TAIWAN", MIN_ARTICLES - 1, BASELINE_AVG)])
        rule.es.search.side_effect = [short, baseline]
        assert rule.run() == []

    def test_pair_missing_from_baseline_no_fire(self):
        rule = _rule()
        short = _agg_resp(
            [_bucket("CHINA", "TAIWAN", MIN_ARTICLES + 40, _short_avg(STDDEV_THRESHOLD + 1.0))]
        )
        baseline = _agg_resp([_bucket("RUSSIA", "UKRAINE", MIN_ARTICLES + 40, BASELINE_AVG)])
        rule.es.search.side_effect = [short, baseline]
        assert rule.run() == []


class TestRunEmptyData:
    def test_no_short_window_data_returns_empty(self):
        rule = _rule()
        rule.es.search.return_value = _agg_resp([])
        assert rule.run() == []
        # Short-circuits before the baseline (long-window) aggregation.
        assert rule.es.search.call_count == 1

    def test_es_error_returns_empty(self):
        rule = _rule()
        rule.es.search.side_effect = RuntimeError("es down")
        assert rule.run() == []


class TestAggregateTone:
    def test_reciprocal_buckets_merge_weighted(self):
        """A->B and B->A buckets collapse into one pair with a weighted avg."""
        rule = _rule()
        rule.es.search.return_value = _agg_resp([
            _bucket("CHINA", "TAIWAN", 30, -4.0, std=1.0, mn=-7.0, mx=1.0),
            _bucket("TAIWAN", "CHINA", 10, -8.0, std=2.0, mn=-9.0, mx=0.0),
        ])
        stats = rule._aggregate_tone(days=SHORT_WINDOW_DAYS)
        assert list(stats) == ["CHINA||TAIWAN"]
        merged = stats["CHINA||TAIWAN"]
        assert merged["count"] == 40
        assert merged["avg_tone"] == pytest.approx(-5.0)  # (30*-4 + 10*-8) / 40
        assert merged["std_tone"] == pytest.approx(2.0)  # max of the two stds
        assert merged["min_tone"] == -9.0
        assert merged["max_tone"] == 1.0

    def test_pagination_follows_after_key(self):
        rule = _rule()
        page1 = _agg_resp(
            [_bucket("CHINA", "TAIWAN", 20, -3.0)],
            after_key={"src": "CHINA", "tgt": "TAIWAN"},
        )
        page2 = _agg_resp([_bucket("RUSSIA", "UKRAINE", 15, -1.0)])
        rule.es.search.side_effect = [page1, page2]
        stats = rule._aggregate_tone(days=LONG_WINDOW_DAYS)
        assert set(stats) == {"CHINA||TAIWAN", "RUSSIA||UKRAINE"}
        assert rule.es.search.call_count == 2
        # The second request must carry the composite cursor.
        kwargs = rule.es.search.call_args.kwargs
        assert kwargs["aggs"]["country_pairs"]["composite"]["after"] == {
            "src": "CHINA", "tgt": "TAIWAN",
        }

    def test_zero_count_bucket_skipped(self):
        rule = _rule()
        rule.es.search.return_value = _agg_resp([_bucket("CHINA", "TAIWAN", 0, 0.0)])
        assert rule._aggregate_tone(days=SHORT_WINDOW_DAYS) == {}


class TestComputeDeviation:
    def test_zero_std_floored_not_none(self):
        # std=0 (syndicated copies of one article) is floored, not skipped:
        # the shift is still real, only its scale was untrustworthy.
        assert RhetoricShiftRule._compute_deviation(
            -5.0, -2.0, 0.0) == pytest.approx(-3.0)

    def test_nan_std_returns_none(self):
        assert RhetoricShiftRule._compute_deviation(-5.0, -2.0, float("nan")) is None

    def test_signed_sigmas(self):
        assert RhetoricShiftRule._compute_deviation(-5.0, -2.0, 1.5) == pytest.approx(-2.0)
        assert RhetoricShiftRule._compute_deviation(1.0, -2.0, 1.5) == pytest.approx(2.0)

    def test_near_zero_std_no_blowup(self):
        """First production backtest: NEW ZEALAND||VIETNAM at z=26301
        because the baseline tone variance of a sparse, syndication-fed
        pair was ~0.0001. The floor caps the scale at |Δtone| sigmas."""
        dev = RhetoricShiftRule._compute_deviation(-5.0, -2.0, 0.0001)
        assert abs(dev) == pytest.approx(3.0)
        assert abs(dev) < 50

    def test_floor_inactive_above_one(self):
        assert RhetoricShiftRule._compute_deviation(
            -5.0, -2.0, 3.0) == pytest.approx(-1.0)


class TestSeverityTiers:
    """Severity for this weak-signal rule is capped at high (>= 4 sigma)."""

    @pytest.mark.parametrize(
        ("deviation", "expected"),
        [
            (-4.5, "high"),
            (-4.0, "high"),
            (-3.5, "medium"),
            (-3.0, "medium"),
            (-2.5, "low"),
            (2.5, "low"),  # improving rhetoric also fires (abs deviation)
            (4.2, "high"),
        ],
    )
    def test_tiers(self, deviation, expected):
        rule = object.__new__(RhetoricShiftRule)
        rule.as_of = None  # injectable clock (backtesting)
        short = {"avg_tone": -6.0, "std_tone": 1.0, "count": 50,
                 "min_tone": -9.0, "max_tone": -2.0,
                 "source_country": "CHINA", "target_country": "TAIWAN"}
        baseline = {"avg_tone": -2.0, "std_tone": 1.5, "count": 300,
                    "min_tone": -8.0, "max_tone": 3.0,
                    "source_country": "CHINA", "target_country": "TAIWAN"}
        corr = rule._build_correlation("CHINA||TAIWAN", short, baseline, deviation)
        assert corr["severity"] == expected
        direction = "improving" if deviation > 0 else "deteriorating"
        assert direction in corr["description"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
