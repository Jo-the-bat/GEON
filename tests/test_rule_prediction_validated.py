"""Tests for Correlation Rule 8: prediction market movement + real event.

A significant Polymarket price shift (filtered in ES by
PRICE_SHIFT_THRESHOLD) validated by a high-severity GDELT event
(|Goldstein| > GOLDSTEIN_SEVERITY) for the same countries.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingestors"))

import pytest
from correlation.rules.prediction_validated import (
    GDELT_INDEX_PATTERN,
    GOLDSTEIN_SEVERITY,
    POLYMARKET_INDEX,
    PRICE_SHIFT_THRESHOLD,
    PredictionValidatedRule,
)

EVENT_DATE = "2026-06-10T12:00:00+00:00"
DATE_AFTER_EVENT = "2026-06-11T00:00:00+00:00"
DATE_BEFORE_EVENT = "2026-06-09T00:00:00+00:00"


def _make_rule(es):
    rule = object.__new__(PredictionValidatedRule)
    rule.es = es
    return rule


GDELT_CONCRETE_INDEX = "geon-gdelt-events-2026.06"


def _hits(sources, index=GDELT_CONCRETE_INDEX):
    return {"hits": {"hits": [{"_source": s, "_id": str(i), "_index": index}
                              for i, s in enumerate(sources)]}}


def _case(case_id="case-1", countries=("RUSSIA",), date=DATE_AFTER_EVENT,
          change_24h=PRICE_SHIFT_THRESHOLD + 0.05, change_7d=0.0, price=0.62):
    return {
        "case_id": case_id,
        "question": "Will Russia escalate before July?",
        "countries_involved": list(countries),
        "status": "active",
        "date": date,
        "outcome_yes_price": price,
        "price_change_24h": change_24h,
        "price_change_7d": change_7d,
    }


def _event(event_id="1001", goldstein=-(GOLDSTEIN_SEVERITY + 1.0),
           date=EVENT_DATE, desc="Military force deployment"):
    return {
        "event_id": event_id,
        "date": date,
        "goldstein_scale": goldstein,
        "cameo_description": desc,
    }


def _es_with(cases, events):
    es = MagicMock()

    def search(index=None, **kwargs):
        if index == POLYMARKET_INDEX:
            return _hits(cases, index=POLYMARKET_INDEX)
        if index == GDELT_INDEX_PATTERN:
            return _hits(events)
        raise AssertionError(f"unexpected index queried: {index}")

    es.search.side_effect = search
    return es


class TestHappyPath:
    def test_mover_plus_event_yields_correlation(self):
        es = _es_with([_case()], [_event()])

        correlations = _make_rule(es).run()

        assert len(correlations) == 1
        corr = correlations[0]
        assert corr["rule_name"] == "prediction_event_match"
        assert corr["severity"] == "high"  # case date after event = reaction
        assert corr["countries_involved"] == ["RUSSIA"]
        assert len(corr["correlation_id"]) == 20
        assert "Will Russia escalate" in corr["description"]
        assert [t["type"] for t in corr["timeline"]] == [
            "prediction_market", "geopolitical_event",
        ]
        assert corr["diplomatic_event"]["event_id"] == "1001"
        assert corr["diplomatic_event"]["goldstein"] == -(GOLDSTEIN_SEVERITY + 1.0)
        # Evidence + confidence contract.
        assert isinstance(corr["confidence"], int)
        assert 5 <= corr["confidence"] <= 95
        assert isinstance(corr["confidence_factors"], dict)
        assert "base" in corr["confidence_factors"]
        assert corr["evidence"]
        for entry in corr["evidence"]:
            assert {"index", "doc_id", "kind", "summary"} <= set(entry)

    def test_anticipation_when_market_moved_first_is_medium(self):
        es = _es_with([_case(date=DATE_BEFORE_EVENT)], [_event()])

        correlations = _make_rule(es).run()

        assert len(correlations) == 1
        assert correlations[0]["severity"] == "medium"
        assert "anticipation" in correlations[0]["description"]


class TestGating:
    def test_no_high_severity_event_no_correlation(self):
        es = _es_with([_case()], [])

        assert _make_rule(es).run() == []

    def test_case_without_countries_skipped_before_gdelt_query(self):
        es = _es_with([_case(countries=())], [_event()])

        assert _make_rule(es).run() == []
        # Only the Polymarket search ran; GDELT was never queried.
        assert es.search.call_count == 1

    def test_no_movers_returns_empty(self):
        es = _es_with([], [])

        assert _make_rule(es).run() == []

    def test_polymarket_query_failure_returns_empty(self):
        es = MagicMock()
        es.search.side_effect = RuntimeError("es down")

        assert _make_rule(es).run() == []


class TestQueryThresholdWiring:
    def test_price_shift_threshold_in_polymarket_query(self):
        es = _es_with([], [])

        _make_rule(es).run()

        query = es.search.call_args.kwargs["query"]
        ranges = [c["bool"]["must"][1]["range"] for c in query["bool"]["should"]]
        assert {"price_change_24h": {"gt": PRICE_SHIFT_THRESHOLD}} in ranges
        assert {"price_change_24h": {"lt": -PRICE_SHIFT_THRESHOLD}} in ranges
        assert {"price_change_7d": {"gt": PRICE_SHIFT_THRESHOLD}} in ranges
        assert {"price_change_7d": {"lt": -PRICE_SHIFT_THRESHOLD}} in ranges

    def test_goldstein_severity_in_gdelt_query(self):
        es = _es_with([_case()], [_event()])

        _make_rule(es).run()

        gdelt_query = es.search.call_args_list[1].kwargs["query"]
        should = gdelt_query["bool"]["should"]
        assert {"range": {"goldstein_scale": {"lt": -GOLDSTEIN_SEVERITY}}} in should
        assert {"range": {"goldstein_scale": {"gt": GOLDSTEIN_SEVERITY}}} in should


class TestBuildDetails:
    def test_worst_event_selected_by_lowest_goldstein(self):
        mild = _event(event_id="1", goldstein=-(GOLDSTEIN_SEVERITY + 1.0))
        worst = _event(event_id="2", goldstein=-(GOLDSTEIN_SEVERITY + 2.5),
                       desc="Use of conventional military force")
        es = _es_with([_case()], [mild, worst])

        correlations = _make_rule(es).run()

        diplo = correlations[0]["diplomatic_event"]
        assert diplo["event_id"] == "2"
        assert diplo["goldstein"] == -(GOLDSTEIN_SEVERITY + 2.5)
        assert diplo["description"] == "Use of conventional military force"

    def test_null_price_change_coalesced_to_zero(self):
        # price_change_24h can be null (insufficient history): the build
        # must not crash and must fall back to the 7d change.
        change_7d = PRICE_SHIFT_THRESHOLD + 0.15
        es = _es_with([_case(change_24h=None, change_7d=change_7d)], [_event()])

        correlations = _make_rule(es).run()

        assert len(correlations) == 1
        assert f"{change_7d:+.1%}" in correlations[0]["description"]

    def test_unparseable_dates_fall_back_to_reaction_severity(self):
        es = _es_with([_case(date="not-a-date")], [_event(date="also-bad")])

        correlations = _make_rule(es).run()

        assert len(correlations) == 1
        assert correlations[0]["severity"] == "high"
        assert "unknown" in correlations[0]["description"]
        # No measurable gap between the dates -> no proximity bonus.
        assert correlations[0]["confidence_factors"]["proximity"] == 0.0


class TestEvidenceAndConfidence:
    def test_evidence_references_case_and_event_hits(self):
        es = _es_with([_case()], [_event()])

        corr = _make_rule(es).run()[0]

        market_ev = corr["evidence"][0]
        assert market_ev["kind"] == "market"
        assert market_ev["index"] == POLYMARKET_INDEX
        assert market_ev["doc_id"] == "case-1"
        assert "Will Russia escalate" in market_ev["summary"]

        event_ev = corr["evidence"][1]
        assert event_ev["kind"] == "diplomatic"
        # The GDELT hit ids/_index are kept, not dropped.
        assert event_ev["index"] == GDELT_CONCRETE_INDEX
        assert event_ev["doc_id"] == "0"
        assert event_ev["date"] == EVENT_DATE
        assert "Military force deployment" in event_ev["summary"]

    def test_event_evidence_capped_at_five(self):
        events = [_event(event_id=str(i)) for i in range(8)]
        es = _es_with([_case()], events)

        corr = _make_rule(es).run()[0]

        assert len(corr["evidence"]) == 6  # 1 market case + 5 events

    def test_confidence_factors_are_auditable(self):
        es = _es_with([_case()], [_event()])

        corr = _make_rule(es).run()[0]

        factors = corr["confidence_factors"]
        assert set(factors) == {"base", "volume", "proximity", "market_move"}
        assert factors["base"] == 30.0
        assert corr["confidence"] == min(95, max(5, round(sum(factors.values()))))

    def test_proximity_higher_when_market_and_event_coincide(self):
        near = _make_rule(_es_with([_case(date=EVENT_DATE)], [_event()])).run()[0]
        # ~66h gap, still inside the 72h window.
        far = _make_rule(
            _es_with([_case(date="2026-06-13T06:00:00+00:00")], [_event()])
        ).run()[0]

        assert (near["confidence_factors"]["proximity"]
                > far["confidence_factors"]["proximity"])
        assert near["confidence"] > far["confidence"]

    def test_market_move_factor_scales_and_caps(self):
        small = _make_rule(
            _es_with([_case(change_24h=PRICE_SHIFT_THRESHOLD + 0.02)], [_event()])
        ).run()[0]
        big = _make_rule(
            _es_with([_case(change_24h=PRICE_SHIFT_THRESHOLD + 0.50)], [_event()])
        ).run()[0]

        assert (small["confidence_factors"]["market_move"]
                < big["confidence_factors"]["market_move"])
        assert big["confidence_factors"]["market_move"] == 15.0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
