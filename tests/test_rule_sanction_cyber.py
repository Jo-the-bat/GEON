"""Unit tests for correlation Rule 2: sanctions + cyber indicator spike.

Pure logic, mocked Elasticsearch and monkeypatched OpenCTI helper — no
live network calls. The severity matrix and the _compute_ioc_spike unit
behaviour are covered in test_correlation_engine.py; id stability in
test_correlation_ids.py. This file covers run() end-to-end: sanction
discovery, country grouping/normalization, the spike threshold and the
MIN_BASELINE_COUNT guard.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingestors"))

import pytest
from correlation.rules.sanction_cyber import (
    IOC_SPIKE_THRESHOLD,
    IOC_WINDOW_DAYS,
    MIN_BASELINE_COUNT,
    SanctionCyberRule,
)

SEVERITIES = {"low", "medium", "high", "critical"}
BASELINE = max(MIN_BASELINE_COUNT, 10) * 10  # comfortably valid baseline


def _sanction(country: str = "RUSSIA", name: str = "Entity X") -> dict[str, Any]:
    return {
        "name": name,
        "country": country,
        "sanctions_source": "OFAC",
        "programs": ["UKRAINE-EO13662"],
        "ingested_at": "2026-06-10T00:00:00+00:00",
    }


def _hits(sources: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap _source docs in the exact ES search response shape."""
    return {
        "hits": {
            "hits": [
                {"_id": f"doc-{i}", "_source": s} for i, s in enumerate(sources)
            ]
        }
    }


def _rule(
    sanctions: list[dict[str, Any]],
    counts: list[int] | None = None,
) -> SanctionCyberRule:
    """Build a rule with mocked ES.

    ``counts`` feeds es.count responses in call order: the rule counts the
    post-sanction window first, then the baseline window, per country.
    """
    es = MagicMock()
    es.search.return_value = _hits(sanctions)
    if counts is not None:
        es.count.side_effect = [{"count": c} for c in counts]
    else:
        es.count.return_value = {"count": 0}
    return SanctionCyberRule(es=es, octi=None)


@pytest.fixture(autouse=True)
def _no_opencti_fallback(monkeypatch):
    """Keep the OpenCTI fallback inert by default (ES counts only)."""
    monkeypatch.setattr(
        "correlation.rules.sanction_cyber.get_indicators_by_country",
        lambda octi, country, days_back=60: [],
    )


class TestHappyPath:
    def test_sanction_followed_by_spike_yields_correlation(self):
        post = int(BASELINE * IOC_SPIKE_THRESHOLD) + 1
        rule = _rule([_sanction()], counts=[post, BASELINE])

        correlations = rule.run()

        assert len(correlations) == 1
        corr = correlations[0]
        assert corr["rule_name"] == "sanction_cyber_spike"
        assert corr["severity"] in SEVERITIES
        assert corr["countries_involved"] == ["RUSSIA"]
        assert len(corr["correlation_id"]) == 20
        assert "RUSSIA" in corr["description"]
        assert str(IOC_WINDOW_DAYS) in corr["description"]
        assert "UKRAINE-EO13662" in corr["description"]
        # Timeline: the sanction entry plus the closing cyber-spike entry.
        types = [t["type"] for t in corr["timeline"]]
        assert types.count("sanction") == 1
        assert types[-1] == "cyber"
        assert "Entity X" in corr["timeline"][0]["description"]

    def test_ratio_exactly_at_threshold_correlates(self):
        """The spike comparison is inclusive (>= IOC_SPIKE_THRESHOLD)."""
        post = int(BASELINE * IOC_SPIKE_THRESHOLD)
        rule = _rule([_sanction()], counts=[post, BASELINE])
        assert len(rule.run()) == 1


class TestThresholdEdge:
    def test_ratio_just_below_threshold_no_correlation(self):
        post = int(BASELINE * IOC_SPIKE_THRESHOLD) - 1
        rule = _rule([_sanction()], counts=[post, BASELINE])
        assert rule.run() == []


class TestMinBaselineGuard:
    def test_tiny_baseline_yields_no_correlation(self):
        """A near-zero baseline must NOT be reported as a giant spike —
        the comparison is skipped entirely (ratio is None)."""
        rule = _rule([_sanction()], counts=[100_000, MIN_BASELINE_COUNT - 1])
        assert rule.run() == []

    def test_baseline_exactly_at_minimum_is_accepted(self):
        post = int(MIN_BASELINE_COUNT * IOC_SPIKE_THRESHOLD) + 1
        rule = _rule([_sanction()], counts=[post, MIN_BASELINE_COUNT])
        assert len(rule.run()) == 1


class TestEmptyData:
    def test_no_recent_sanctions_returns_empty(self):
        rule = _rule([])
        assert rule.run() == []
        rule.es.count.assert_not_called()

    def test_sanctions_query_failure_returns_empty(self):
        rule = _rule([])
        rule.es.search.side_effect = RuntimeError("es down")
        assert rule.run() == []

    def test_sanction_without_country_is_skipped(self):
        rule = _rule([_sanction(country="")])
        assert rule.run() == []
        rule.es.count.assert_not_called()


class TestCountryGrouping:
    def test_vendor_spelling_normalized_and_grouped(self):
        """Pre-migration docs ('RUSSIAN FEDERATION') must merge with the
        canonical spelling into ONE correlation for RUSSIA."""
        post = int(BASELINE * IOC_SPIKE_THRESHOLD) + 1
        rule = _rule(
            [
                _sanction(country="RUSSIAN FEDERATION", name="Entity A"),
                _sanction(country="RUSSIA", name="Entity B"),
            ],
            counts=[post, BASELINE],  # one country -> exactly two count calls
        )

        correlations = rule.run()
        assert len(correlations) == 1
        assert correlations[0]["countries_involved"] == ["RUSSIA"]
        assert rule.es.count.call_count == 2

    def test_ioc_counts_query_canonical_country_term(self):
        post = int(BASELINE * IOC_SPIKE_THRESHOLD) + 1
        rule = _rule([_sanction(country="RUSSIAN FEDERATION")],
                     counts=[post, BASELINE])
        rule.run()
        for call in rule.es.count.call_args_list:
            must = call.kwargs["query"]["bool"]["must"]
            assert {"term": {"country": "RUSSIA"}} in must


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
