"""Tests for Correlation Rule 7: arms transfer + regional escalation.

The rule compares negative GDELT event counts between an arms recipient
and each of its neighbours in the ESCALATION_WINDOW_DAYS before vs after
the delivery, firing when the increase reaches ESCALATION_THRESHOLD_PCT.
"""

import math
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingestors"))

import pytest
from correlation.rules.arms_escalation import (
    ESCALATION_THRESHOLD_PCT,
    ESCALATION_WINDOW_DAYS,
    TRANSFERS_INDEX,
    ArmsEscalationRule,
)

# Real entry in ingestors/common/country_neighbors.json:
# INDIA -> BANGLADESH, CHINA, MYANMAR, NEPAL, PAKISTAN
RECIPIENT = "INDIA"
NEIGHBOR = "PAKISTAN"
REF_DATE = "2026-01-15T00:00:00+00:00"


def _make_rule(es):
    rule = object.__new__(ArmsEscalationRule)
    rule.es = es
    return rule


def _transfer(recipient=RECIPIENT, date=REF_DATE, tiv=120.0,
              supplier="FRANCE", weapon="Rafale combat aircraft",
              transfer_id="t-0001"):
    return {
        "transfer_id": transfer_id,
        "recipient_country": recipient,
        "supplier_country": supplier,
        "weapon_description": weapon,
        "weapon_type": "Aircraft",
        "tiv_value": tiv,
        "date": date,
    }


def _search_response(sources):
    return {"hits": {"hits": [{"_source": s, "_id": str(i)}
                              for i, s in enumerate(sources)]}}


def _count_side_effect(counts, ref_date=REF_DATE):
    """es.count stub keyed on (neighbor, "before"|"after").

    Mirrors the exact query shape the rule builds: filter[0] is the date
    range, filter[2] holds the symmetric country-pair clauses. The
    "before" window is the one whose range ends at the reference date.
    """
    def _count(index=None, body=None, **kwargs):
        filters = body["query"]["bool"]["filter"]
        date_range = filters[0]["range"]["date"]
        pair = filters[2]["bool"]["should"][0]["bool"]["must"]
        neighbor = pair[1]["term"]["target_country"]
        phase = "before" if date_range["lte"] == ref_date else "after"
        return {"count": counts.get((neighbor, phase), 0)}
    return _count


def _es_with(transfers, counts, ref_date=REF_DATE):
    es = MagicMock()
    es.search.return_value = _search_response(transfers)
    es.count.side_effect = _count_side_effect(counts, ref_date)
    return es


def _after_at_threshold(before: int) -> int:
    """Smallest 'after' count whose increase is >= the threshold pct."""
    return math.ceil(before * (1 + ESCALATION_THRESHOLD_PCT / 100))


def _after_below_threshold(before: int) -> int:
    """Largest 'after' count whose increase is strictly below threshold."""
    return math.floor(before * (1 + ESCALATION_THRESHOLD_PCT / 100)) - 1


class TestHappyPath:
    def test_escalation_at_threshold_fires(self):
        before = 200
        es = _es_with(
            [_transfer()],
            {(NEIGHBOR, "before"): before,
             (NEIGHBOR, "after"): _after_at_threshold(before)},
        )

        correlations = _make_rule(es).run()

        # Only PAKISTAN escalated; the four quiet neighbours stay silent.
        assert len(correlations) == 1
        corr = correlations[0]
        assert corr["rule_name"] == "arms_transfer_escalation"
        assert corr["severity"] in ("high", "critical")
        assert corr["countries_involved"] == sorted([RECIPIENT, NEIGHBOR])
        assert len(corr["correlation_id"]) == 20
        assert RECIPIENT in corr["description"]
        assert NEIGHBOR in corr["description"]
        assert f"{ESCALATION_WINDOW_DAYS}d" in corr["description"]
        assert [t["type"] for t in corr["timeline"]] == ["arms_transfer", "escalation"]
        assert corr["timeline"][0]["date"] == REF_DATE
        # Evidence + confidence contract.
        assert isinstance(corr["confidence"], int)
        assert 5 <= corr["confidence"] <= 95
        assert isinstance(corr["confidence_factors"], dict)
        assert "base" in corr["confidence_factors"]
        assert corr["evidence"]
        for entry in corr["evidence"]:
            assert {"index", "doc_id", "kind", "summary"} <= set(entry)

    def test_all_neighbors_compared_before_and_after(self):
        es = _es_with([_transfer()], {})

        _make_rule(es).run()

        # INDIA has 5 neighbours in the real mapping -> 2 count calls each.
        assert es.count.call_count == 10


class TestThresholdEdge:
    def test_increase_just_below_threshold_no_correlation(self):
        before = 200
        es = _es_with(
            [_transfer()],
            {(NEIGHBOR, "before"): before,
             (NEIGHBOR, "after"): _after_below_threshold(before)},
        )

        assert _make_rule(es).run() == []

    def test_decrease_no_correlation(self):
        es = _es_with(
            [_transfer()],
            {(NEIGHBOR, "before"): 50, (NEIGHBOR, "after"): 10},
        )

        assert _make_rule(es).run() == []


class TestEmptyData:
    def test_no_transfers_returns_empty(self):
        es = _es_with([], {})

        assert _make_rule(es).run() == []
        es.count.assert_not_called()

    def test_transfers_query_failure_returns_empty(self):
        es = MagicMock()
        es.search.side_effect = RuntimeError("es down")

        assert _make_rule(es).run() == []

    def test_recipient_without_neighbors_skipped(self):
        es = _es_with([_transfer(recipient="WAKANDA")], {})

        assert _make_rule(es).run() == []
        es.count.assert_not_called()

    def test_quiet_pair_zero_before_zero_after_skipped(self):
        # All neighbours report 0/0 -> no division, no correlation.
        es = _es_with([_transfer()], {})

        assert _make_rule(es).run() == []


class TestSeverityAndGrouping:
    def test_more_than_doubling_is_critical(self):
        # 10 -> 30 = +200%: above the default threshold and above the
        # hardcoded 100% boundary for critical severity.
        es = _es_with(
            [_transfer()],
            {(NEIGHBOR, "before"): 10, (NEIGHBOR, "after"): 30},
        )

        correlations = _make_rule(es).run()
        assert len(correlations) == 1
        assert correlations[0]["severity"] == "critical"

    def test_cold_start_capped_at_100pct_stays_high(self):
        # before=0, after>0 is treated as a flat +100% increase, so a
        # cold-start explosion can never be rated critical (pct > 100).
        es = _es_with(
            [_transfer()],
            {(NEIGHBOR, "before"): 0, (NEIGHBOR, "after"): 40},
        )

        correlations = _make_rule(es).run()
        assert len(correlations) == 1
        assert correlations[0]["severity"] == "high"
        assert "100%" in correlations[0]["description"]

    def test_highest_tiv_transfer_describes_the_correlation(self):
        # Two deliveries to the same recipient: the earliest date anchors
        # the window, the highest-TIV transfer provides the narrative.
        small = _transfer(date="2026-03-01T00:00:00+00:00", tiv=50.0,
                          supplier="ISRAEL", weapon="Light helicopters")
        big = _transfer(date=REF_DATE, tiv=500.0,
                        supplier="FRANCE", weapon="Scorpene submarine")
        es = _es_with(
            [small, big],
            {(NEIGHBOR, "before"): 10, (NEIGHBOR, "after"): 30},
            ref_date=REF_DATE,  # min(date) of the two transfers
        )

        correlations = _make_rule(es).run()
        assert len(correlations) == 1
        assert "Scorpene submarine" in correlations[0]["description"]
        assert "FRANCE" in correlations[0]["description"]


class TestEvidenceAndConfidence:
    def test_evidence_references_transfer_and_signal(self):
        es = _es_with(
            [_transfer()],
            {(NEIGHBOR, "before"): 10, (NEIGHBOR, "after"): 30},
        )

        corr = _make_rule(es).run()[0]

        kinds = [e["kind"] for e in corr["evidence"]]
        assert kinds == ["transfer", "signal"]
        transfer_ev = corr["evidence"][0]
        assert transfer_ev["index"] == TRANSFERS_INDEX
        assert transfer_ev["doc_id"] == "t-0001"
        assert transfer_ev["date"] == REF_DATE
        assert "Rafale" in transfer_ev["summary"]
        signal_ev = corr["evidence"][1]
        assert "10 before vs 30 after" in signal_ev["summary"]
        assert NEIGHBOR in signal_ev["summary"]

    def test_confidence_factors_are_auditable(self):
        # 10 -> 30 = +200%: escalation_strength caps at 20, the
        # post-delivery volume contributes, and the recorded factors sum
        # (clamped) to the published confidence.
        es = _es_with(
            [_transfer()],
            {(NEIGHBOR, "before"): 10, (NEIGHBOR, "after"): 30},
        )

        corr = _make_rule(es).run()[0]

        factors = corr["confidence_factors"]
        assert set(factors) == {"base", "volume", "escalation_strength"}
        assert factors["base"] == 30.0
        assert factors["escalation_strength"] == 20.0
        assert factors["volume"] > 0
        assert corr["confidence"] == min(95, max(5, round(sum(factors.values()))))

    def test_escalation_strength_zero_at_exact_threshold(self):
        before = 200
        es = _es_with(
            [_transfer()],
            {(NEIGHBOR, "before"): before,
             (NEIGHBOR, "after"): _after_at_threshold(before)},
        )

        corr = _make_rule(es).run()[0]

        # ceil() can overshoot the exact threshold slightly — the factor
        # must stay proportionally tiny, never negative.
        assert 0 <= corr["confidence_factors"]["escalation_strength"] < 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
