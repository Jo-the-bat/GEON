"""Tests for situation-stable correlation IDs (Phase 0).

The same ongoing situation must produce the SAME correlation_id across
runs (previously six rules embedded the current date in the hash, so an
ongoing escalation re-alerted every day).
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingestors"))

import pytest

from correlation.rules.diplomatic_apt import DiplomaticAPTRule
from correlation.rules.multi_signal_convergence import MultiSignalConvergenceRule
from correlation.rules.prediction_validated import PredictionValidatedRule
from correlation.rules.rhetoric_shift import RhetoricShiftRule
from correlation.rules.sanction_cyber import SanctionCyberRule


def _make(rule_cls):
    """Instantiate a rule without touching ES/OpenCTI."""
    rule = object.__new__(rule_cls)
    rule.es = None
    rule.octi = None
    return rule


WORST_EVENT = {
    "event_id": "1234567890",
    "date": "2026-06-01T12:00:00+00:00",
    "goldstein_scale": -8.0,
    "cameo_description": "Military force deployment",
}
APT_MATCHES = [{"name": "APT28", "id": "intrusion-set--abc", "confidence": 85,
                "_geon_type": "intrusion-set", "modified": "2026-06-02"}]


class TestDiplomaticAPTIds:
    def test_stable_across_calls(self):
        rule = _make(DiplomaticAPTRule)
        c1 = rule._build_correlation("RUSSIA", "UKRAINE", WORST_EVENT, APT_MATCHES)
        time.sleep(0.01)
        c2 = rule._build_correlation("RUSSIA", "UKRAINE", WORST_EVENT, APT_MATCHES)
        assert c1["correlation_id"] == c2["correlation_id"]

    def test_pair_order_invariant(self):
        rule = _make(DiplomaticAPTRule)
        c1 = rule._build_correlation("RUSSIA", "UKRAINE", WORST_EVENT, APT_MATCHES)
        c2 = rule._build_correlation("UKRAINE", "RUSSIA", WORST_EVENT, APT_MATCHES)
        assert c1["correlation_id"] == c2["correlation_id"]

    def test_different_pairs_differ(self):
        rule = _make(DiplomaticAPTRule)
        c1 = rule._build_correlation("RUSSIA", "UKRAINE", WORST_EVENT, APT_MATCHES)
        c2 = rule._build_correlation("CHINA", "TAIWAN", WORST_EVENT, APT_MATCHES)
        assert c1["correlation_id"] != c2["correlation_id"]


class TestSanctionCyberIds:
    def test_stable_per_country(self):
        rule = _make(SanctionCyberRule)
        docs = [{"name": "Entity X", "sanctions_source": "OFAC",
                 "programs": ["UKRAINE-EO13662"], "ingested_at": "2026-06-01"}]
        c1 = rule._build_correlation("RUSSIA", docs, 3.5)
        c2 = rule._build_correlation("RUSSIA", docs, 4.0)
        assert c1["correlation_id"] == c2["correlation_id"]


class TestRhetoricShiftIds:
    def test_stable_per_pair(self):
        rule = _make(RhetoricShiftRule)
        short = {"avg_tone": -6.0, "std_tone": 1.0, "count": 50,
                 "min_tone": -9.0, "max_tone": -2.0,
                 "source_country": "CHINA", "target_country": "TAIWAN"}
        baseline = {"avg_tone": -2.0, "std_tone": 1.5, "count": 300,
                    "min_tone": -8.0, "max_tone": 3.0,
                    "source_country": "CHINA", "target_country": "TAIWAN"}
        c1 = rule._build_correlation("CHINA||TAIWAN", short, baseline, -2.7)
        c2 = rule._build_correlation("CHINA||TAIWAN", short, baseline, -3.1)
        assert c1["correlation_id"] == c2["correlation_id"]


class TestPredictionValidatedIds:
    def test_anchored_on_case_and_event(self):
        rule = _make(PredictionValidatedRule)
        case = {"case_id": "case-1", "question": "Will X invade Y?",
                "countries_involved": ["RUSSIA"], "outcome_yes_price": 0.6,
                "price_change_24h": 0.15, "price_change_7d": 0.05,
                "date": "2026-06-01T00:00:00+00:00"}
        events = [dict(WORST_EVENT)]
        c1 = rule._build_correlation(case, events)
        c2 = rule._build_correlation(case, events)
        assert c1["correlation_id"] == c2["correlation_id"]

    def test_different_event_differs(self):
        rule = _make(PredictionValidatedRule)
        case = {"case_id": "case-1", "question": "Will X invade Y?",
                "countries_involved": ["RUSSIA"], "outcome_yes_price": 0.6,
                "price_change_24h": 0.15, "price_change_7d": 0.05,
                "date": "2026-06-01T00:00:00+00:00"}
        other_event = dict(WORST_EVENT, event_id="999")
        c1 = rule._build_correlation(case, [dict(WORST_EVENT)])
        c2 = rule._build_correlation(case, [other_event])
        assert c1["correlation_id"] != c2["correlation_id"]


class TestMultiSignalIds:
    def test_stable_per_country(self):
        rule = _make(MultiSignalConvergenceRule)
        signals = {
            "gdelt_negative_events": 150,
            "sanctions_recent": True,
            "internet_outage": True,
            "prediction_market_movement": "",
            "apt_activity": "",
            "acled_conflicts": 0,
            "military_spending_increase": False,
        }
        risk_doc = {"country": "SYRIA", "risk_score": 62.0}
        c1 = rule._build_correlation("SYRIA", risk_doc, signals, 3)
        time.sleep(0.01)
        c2 = rule._build_correlation("SYRIA", risk_doc, signals, 3)
        assert c1["correlation_id"] == c2["correlation_id"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
