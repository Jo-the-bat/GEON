"""Tests for confidence scoring and statistical baselines (Phase 3)."""

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingestors"))

import pytest
from correlation import scoring
from correlation.baselines import MIN_BASELINE_DAYS, negative_events_baseline


class TestConfidence:
    def test_clamped_floor_and_ceiling(self):
        assert scoring.clamp_confidence(-50) == scoring.CONFIDENCE_FLOOR
        assert scoring.clamp_confidence(500) == scoring.CONFIDENCE_CEILING

    def test_confidence_records_factors(self):
        conf, factors = scoring.confidence(30, {"a": 10.0, "b": 5.0})
        assert conf == 45
        assert factors == {"base": 30.0, "a": 10.0, "b": 5.0}

    def test_attribution_ordering(self):
        assert (scoring.attribution_bonus("opencti")
                > scoring.attribution_bonus("es_cti")
                > scoring.attribution_bonus("static")
                > scoring.attribution_bonus("unknown"))

    def test_volume_saturates(self):
        assert scoring.volume_bonus(0) == 0.0
        assert scoring.volume_bonus(10, saturation=20) == 10.0
        assert scoring.volume_bonus(999, saturation=20) == 20.0

    def test_zscore_below_threshold_is_zero(self):
        assert scoring.zscore_bonus(1.9, threshold=2.0) == 0.0
        assert scoring.zscore_bonus(None) == 0.0
        assert scoring.zscore_bonus(2.0, threshold=2.0) == pytest.approx(10.0)

    def test_proximity_closer_is_higher(self):
        close = scoring.proximity_bonus(1, window_days=30)
        far = scoring.proximity_bonus(29, window_days=30)
        assert close > far > 0
        assert scoring.proximity_bonus(None, 30) == 0.0


class TestEvidence:
    def test_entry_truncates_summary(self):
        e = scoring.evidence_entry(index="i", doc_id="d", date="2026-01-01",
                                   kind="cyber", summary="x" * 999)
        assert len(e["summary"]) == 300

    def test_from_hit(self):
        hit = {"_index": "geon-gdelt-events-2026.06", "_id": "abc",
               "_source": {"date": "2026-06-01", "goldstein_scale": -8}}
        e = scoring.evidence_from_hit(hit, "diplomatic", "Goldstein -8")
        assert e["index"] == "geon-gdelt-events-2026.06"
        assert e["doc_id"] == "abc"
        assert e["date"] == "2026-06-01"


def _agg_response(daily_counts, start):
    """Build a date_histogram response: one bucket per day."""
    day_ms = 24 * 3600 * 1000
    start_ms = start.timestamp() * 1000
    return {
        "aggregations": {
            "daily": {
                "buckets": [
                    {"key": start_ms + i * day_ms, "doc_count": c}
                    for i, c in enumerate(daily_counts)
                ]
            }
        }
    }


NOW = datetime(2026, 6, 12, tzinfo=timezone.utc)


class TestNegativeEventsBaseline:
    def _es(self, daily_counts, baseline_days=90, window_days=7):
        from datetime import timedelta
        es = MagicMock()
        start = NOW - timedelta(days=baseline_days + window_days)
        es.search.return_value = _agg_response(daily_counts, start)
        return es

    def test_flat_history_low_z(self):
        # 90 baseline days at ~10/day, 7-day window also at 10/day.
        counts = [10] * 90 + [10] * 8
        es = self._es(counts)
        b = negative_events_baseline(es, "RUSSIA", "UKRAINE",
                                     window_days=7, baseline_days=90,
                                     as_of=NOW)
        assert b is not None
        assert abs(b.zscore) < 1.0

    def test_spike_high_z(self):
        counts = [10] * 90 + [60] * 8
        es = self._es(counts)
        b = negative_events_baseline(es, "RUSSIA", "UKRAINE",
                                     window_days=7, baseline_days=90,
                                     as_of=NOW)
        assert b is not None
        assert b.zscore > 2.0
        assert b.current_rate > b.baseline_mean

    def test_insufficient_history_returns_none(self):
        # Mostly empty index: leading zeros are trimmed, leaving fewer
        # than MIN_BASELINE_DAYS observed days.
        counts = [0] * 80 + [5] * (MIN_BASELINE_DAYS - 5) + [9] * 8
        es = self._es(counts)
        assert negative_events_baseline(
            es, "RUSSIA", "UKRAINE", window_days=7, baseline_days=90,
            as_of=NOW,
        ) is None

    def test_partial_today_bucket_excluded(self):
        """A replay/run at 14:30 must not let the partial 'today' bucket
        deflate the current rate (review finding: constant ~7/8
        deflation suppressing real spikes near the z-threshold)."""
        from datetime import timedelta
        mid_day = NOW.replace(hour=14, minute=30)
        # 90 baseline days at 10/day, 7 full window days at 60/day, then
        # a nearly-empty partial today bucket.
        counts = [10] * 90 + [60] * 7 + [3]
        es = MagicMock()
        today_floor = mid_day.replace(hour=0, minute=0)
        start = today_floor - timedelta(days=97)
        es.search.return_value = _agg_response(counts, start)
        b = negative_events_baseline(es, "RUSSIA", "UKRAINE",
                                     window_days=7, baseline_days=90,
                                     as_of=mid_day)
        assert b is not None
        assert b.current_rate == pytest.approx(60.0)  # 420 / 7, not /8

    def test_query_failure_returns_none(self):
        es = MagicMock()
        es.search.side_effect = RuntimeError("es down")
        assert negative_events_baseline(es, "RUSSIA", as_of=NOW) is None

    def test_quiet_country_single_event_not_anomalous_blowup(self):
        # std floor: 1 event after dead calm must not be a 50-sigma spike.
        counts = [0] * 5 + [1] + [0] * 84 + [1] + [0] * 7
        es = self._es(counts)
        b = negative_events_baseline(es, "BHUTAN", window_days=7,
                                     baseline_days=90, as_of=NOW)
        if b is not None:
            assert b.zscore < 3.0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
