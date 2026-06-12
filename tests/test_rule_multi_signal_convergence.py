"""Tests for Correlation Rule 10: multi-signal convergence (fusion).

For each country above RISK_SCORE_THRESHOLD the rule checks 7 signals
and fires when at least MIN_SIGNALS converge; >=4 active escalates to
critical, >=5 adds the intelligence_fusion_alert flag.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingestors"))

import pytest
from correlation.rules import multi_signal_convergence as msc
from correlation.rules.multi_signal_convergence import (
    GDELT_NEGATIVE_THRESHOLD,
    MIN_SIGNALS,
    RISK_SCORE_THRESHOLD,
    SPENDING_YOY_THRESHOLD,
    MultiSignalConvergenceRule,
)

COUNTRY = "SYRIA"

_POLY_HIT = {
    "price_change_7d": 0.20,
    "question": "Will the ceasefire collapse before August?",
    "volume": 25000,
}
_CORR_HIT = {"cyber_event": {"apt_group": "APT33"}}
_SPENDING_HIT = {
    "country": COUNTRY,
    "year": 2025,
    "spending_change_yoy_pct": SPENDING_YOY_THRESHOLD + 5.0,
}

# Ordered toggles used to activate exactly N of the 7 signals.
_SIGNAL_TOGGLES = [
    {"sanctions": 1},
    {"outage": 1},
    {"acled": 4},
    {"gdelt": GDELT_NEGATIVE_THRESHOLD},
    {"spending": [_SPENDING_HIT]},
    {"poly": [_POLY_HIT]},
    {"corr": [_CORR_HIT]},
]


def _make_rule(es):
    rule = object.__new__(MultiSignalConvergenceRule)
    rule.es = es
    return rule


def _hits(sources):
    return {"hits": {"hits": [{"_source": s, "_id": str(i)}
                              for i, s in enumerate(sources)]}}


def _risk_doc(country=COUNTRY, score=RISK_SCORE_THRESHOLD + 22.0):
    return {"country": country, "risk_score": score}


def _build_es(risk_docs, *, gdelt=0, sanctions=0, outage=0, acled=0,
              poly=None, corr=None, spending=None):
    """Mock ES answering every index the 7 signal checks query."""
    es = MagicMock()

    def search(index=None, **kwargs):
        if index == msc.RISK_SCORES_INDEX:
            return _hits(risk_docs)
        if index == msc.POLYMARKET_INDEX:
            return _hits(poly or [])
        if index == msc.CORRELATIONS_INDEX:
            return _hits(corr or [])
        if index == msc.SPENDING_INDEX:
            return _hits(spending or [])
        raise AssertionError(f"unexpected search index: {index}")

    def count(index=None, body=None, **kwargs):
        if index == msc.GDELT_INDEX_PATTERN:
            return {"count": gdelt}
        if index == msc.SANCTIONS_INDEX:
            return {"count": sanctions}
        if index == msc.OUTAGES_INDEX:
            return {"count": outage}
        if index == msc.ACLED_INDEX_PATTERN:
            return {"count": acled}
        raise AssertionError(f"unexpected count index: {index}")

    es.search.side_effect = search
    es.count.side_effect = count
    return es


def _es_with_n_signals(n, risk_docs=None):
    kwargs = {}
    for toggles in _SIGNAL_TOGGLES[:n]:
        kwargs.update(toggles)
    return _build_es(risk_docs if risk_docs is not None else [_risk_doc()],
                     **kwargs)


class TestMinSignalsGating:
    def test_below_min_signals_no_correlation(self):
        es = _es_with_n_signals(MIN_SIGNALS - 1)

        assert _make_rule(es).run() == []

    def test_exactly_min_signals_fires(self):
        es = _es_with_n_signals(MIN_SIGNALS)

        correlations = _make_rule(es).run()

        assert len(correlations) == 1
        corr = correlations[0]
        assert corr["rule_name"] == "multi_signal_convergence"
        assert corr["signals_active"] == MIN_SIGNALS
        assert corr["countries_involved"] == [COUNTRY]
        assert len(corr["correlation_id"]) == 20
        assert COUNTRY in corr["description"]
        assert corr["severity"] in ("high", "critical")
        assert corr["timeline"] == []
        assert corr["risk_score"] == RISK_SCORE_THRESHOLD + 22.0
        assert " to " in corr["window"]


class TestSeverityTiers:
    def test_three_signals_high_no_flag(self):
        correlations = _make_rule(_es_with_n_signals(3)).run()

        assert len(correlations) == 1
        assert correlations[0]["severity"] == "high"
        assert "flag" not in correlations[0]

    def test_four_signals_critical_no_flag(self):
        correlations = _make_rule(_es_with_n_signals(4)).run()

        assert len(correlations) == 1
        assert correlations[0]["severity"] == "critical"
        assert "flag" not in correlations[0]

    def test_five_signals_critical_with_fusion_flag(self):
        correlations = _make_rule(_es_with_n_signals(5)).run()

        assert len(correlations) == 1
        assert correlations[0]["severity"] == "critical"
        assert correlations[0]["flag"] == "intelligence_fusion_alert"


class TestSignalThresholdEdges:
    def test_gdelt_count_just_below_threshold_is_inert(self):
        # sanctions + outage active (2) and GDELT one event short of its
        # threshold: the GDELT signal must stay off, so nothing fires.
        es = _build_es([_risk_doc()], sanctions=1, outage=1,
                       gdelt=GDELT_NEGATIVE_THRESHOLD - 1)

        assert _make_rule(es).run() == []

    def test_spending_yoy_at_threshold_is_inert(self):
        # Strict comparison: yoy == threshold does not count as a signal.
        at_threshold = dict(_SPENDING_HIT,
                            spending_change_yoy_pct=SPENDING_YOY_THRESHOLD)
        es = _build_es([_risk_doc()], sanctions=1, outage=1,
                       spending=[at_threshold])

        assert _make_rule(es).run() == []


class TestEmptyData:
    def test_no_high_risk_countries_returns_empty(self):
        es = _build_es([])

        assert _make_rule(es).run() == []
        es.count.assert_not_called()

    def test_risk_scores_query_failure_returns_empty(self):
        es = MagicMock()
        es.search.side_effect = RuntimeError("es down")

        assert _make_rule(es).run() == []

    def test_risk_doc_without_country_skipped(self):
        es = _build_es([{"risk_score": RISK_SCORE_THRESHOLD + 10.0}])

        assert _make_rule(es).run() == []
        es.count.assert_not_called()


class TestSignalsDetail:
    def test_all_seven_signals_reported_in_detail(self):
        correlations = _make_rule(_es_with_n_signals(7)).run()

        assert len(correlations) == 1
        corr = correlations[0]
        assert corr["signals_active"] == 7
        assert corr["flag"] == "intelligence_fusion_alert"

        detail = corr["signals_detail"]
        assert detail["gdelt_negative_events"] == GDELT_NEGATIVE_THRESHOLD
        assert detail["sanctions_recent"] is True
        assert detail["internet_outage"] is True
        assert "Will the ceasefire collapse" in detail["prediction_market_movement"]
        assert detail["apt_activity"] == "APT33"
        assert detail["acled_conflicts"] == 4
        assert detail["military_spending_increase"] is True

        # APT names propagate into the standard cyber_event envelope.
        assert corr["cyber_event"]["apt_group"] == "APT33"
        assert "APT33" in corr["narrative"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
