"""Unit tests for correlation Rule 3: armed conflict + cyber activity.

Pure logic, mocked Elasticsearch and monkeypatched OpenCTI helper — no
live network calls. Covers run() end-to-end (ACLED discovery, country
normalization, correlation assembly), the conflict-type filter, the
severity matrix and timeline capping/sorting.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingestors"))

import pytest
from correlation.rules.conflict_cyber import (
    CONFLICT_EVENT_TYPES,
    CYBER_WINDOW_DAYS,
    ConflictCyberRule,
)

SEVERITIES = {"low", "medium", "high", "critical"}
A_CONFLICT_TYPE = sorted(CONFLICT_EVENT_TYPES)[0]


# SYRIA is used as the default conflict country because it has a real
# entry in country_apt_mapping.json (the rule validates OpenCTI matches
# against the map — unmapped countries can never correlate).
def _acled(
    country: str = "Syria",
    fatalities: int = 5,
    event_date: str = "2026-06-08T00:00:00+00:00",
    location: str = "Aleppo",
) -> dict[str, Any]:
    return {
        "event_id": 42,
        "event_date": event_date,
        "event_type": A_CONFLICT_TYPE,
        "country": country,
        "fatalities": fatalities,
        "location": location,
    }


def _hits(sources: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap _source docs in the exact ES search response shape."""
    return {
        "hits": {
            "hits": [
                {"_id": f"doc-{i}", "_index": "geon-acled-events-2026.06",
                 "_source": s}
                for i, s in enumerate(sources)
            ]
        }
    }


def _campaign(name: str = "Syrian Electronic Army", confidence: int = 70,
              modified: str = "2026-06-09T00:00:00+00:00") -> dict[str, Any]:
    return {
        "name": name,
        "id": f"intrusion-set--{name.lower().replace(' ', '-')}",
        "confidence": confidence,
        "_geon_type": "intrusion-set",
        "modified": modified,
    }


def _rule(acled_sources: list[dict[str, Any]]) -> ConflictCyberRule:
    es = MagicMock()
    es.search.return_value = _hits(acled_sources)
    return ConflictCyberRule(es=es, octi=None)


def _patch_campaigns(monkeypatch: pytest.MonkeyPatch, per_country: dict[str, list]):
    """Patch the helper imported into the rule's module namespace."""
    calls: list[tuple[str, int]] = []

    def fake(octi: Any, country: str, days_back: int = 30) -> list[dict[str, Any]]:
        calls.append((country, days_back))
        return per_country.get(country, [])

    monkeypatch.setattr(
        "correlation.rules.conflict_cyber.get_campaigns_by_country", fake
    )
    return calls


class TestHappyPath:
    def test_conflict_with_cyber_activity_yields_correlation(self, monkeypatch):
        rule = _rule([
            _acled(fatalities=12),
            _acled(fatalities=3, location="Sumy"),
        ])
        calls = _patch_campaigns(monkeypatch, {"SYRIA": [_campaign()]})

        correlations = rule.run()

        assert len(correlations) == 1
        corr = correlations[0]
        assert corr["rule_name"] == "conflict_cyber_infrastructure"
        assert corr["severity"] in SEVERITIES
        assert corr["countries_involved"] == ["SYRIA"]
        assert len(corr["correlation_id"]) == 20
        assert corr["cyber_event"]["apt_group"] == "Syrian Electronic Army"
        # Aggregated conflict stats land in both event and description.
        assert "2 armed-conflict events" in corr["diplomatic_event"]["description"]
        assert "15 total fatalities" in corr["diplomatic_event"]["description"]
        assert "SYRIA" in corr["description"]
        assert "Syrian Electronic Army" in corr["description"]
        types = [t["type"] for t in corr["timeline"]]
        assert types.count("conflict") == 2
        assert types.count("cyber") == 1
        # OpenCTI queried with the configured window for the canonical name.
        assert calls == [("SYRIA", CYBER_WINDOW_DAYS)]
        # Confidence contract: auditable int in [5, 95] with its factors.
        assert isinstance(corr["confidence"], int)
        assert 5 <= corr["confidence"] <= 95
        assert isinstance(corr["confidence_factors"], dict)
        assert "base" in corr["confidence_factors"]
        # Evidence contract: non-empty refs with index/doc_id/kind/summary.
        assert corr["evidence"]
        for ref in corr["evidence"]:
            assert {"index", "doc_id", "kind", "summary"} <= set(ref)

    def test_title_case_country_normalized(self, monkeypatch):
        rule = _rule([_acled(country="Syria")])
        _patch_campaigns(monkeypatch, {"SYRIA": [_campaign()]})
        correlations = rule.run()
        assert correlations[0]["countries_involved"] == ["SYRIA"]


class TestThresholdEdge:
    def test_conflict_without_cyber_activity_yields_nothing(self, monkeypatch):
        rule = _rule([_acled()])
        _patch_campaigns(monkeypatch, {})  # no campaigns anywhere
        assert rule.run() == []

    def test_query_filters_on_configured_conflict_types(self, monkeypatch):
        """The conflict-type cutoff is applied in the ES query: only the
        configured CONFLICT_EVENT_TYPES are requested."""
        rule = _rule([])
        _patch_campaigns(monkeypatch, {})
        rule.run()
        must = rule.es.search.call_args.kwargs["query"]["bool"]["must"]
        terms = next(c for c in must if "terms" in c)
        assert set(terms["terms"]["event_type"]) == set(CONFLICT_EVENT_TYPES)


class TestEmptyData:
    def test_no_acled_hits_returns_empty(self, monkeypatch):
        rule = _rule([])
        fake = MagicMock()
        monkeypatch.setattr(
            "correlation.rules.conflict_cyber.get_campaigns_by_country", fake
        )
        assert rule.run() == []
        fake.assert_not_called()

    def test_acled_query_failure_returns_empty(self, monkeypatch):
        rule = _rule([])
        rule.es.search.side_effect = RuntimeError("es down")
        _patch_campaigns(monkeypatch, {})
        assert rule.run() == []

    def test_event_without_country_is_skipped(self, monkeypatch):
        rule = _rule([_acled(country="")])
        fake = MagicMock()
        monkeypatch.setattr(
            "correlation.rules.conflict_cyber.get_campaigns_by_country", fake
        )
        assert rule.run() == []
        fake.assert_not_called()


class TestAttributionValidation:
    def test_unmapped_apt_is_rejected(self, monkeypatch):
        """OpenCTI matches are validated against country_apt_mapping.json
        (same strict validation as Rules 1 and 6): an APT not attributed
        to the conflict country must NOT correlate."""
        rule = _rule([_acled()])
        _patch_campaigns(monkeypatch, {"SYRIA": [_campaign("Lazarus Group")]})
        assert rule.run() == []

    def test_unmapped_country_never_correlates(self, monkeypatch):
        """A conflict country with no attribution entry cannot be
        validated — no correlation, even if OpenCTI returns matches."""
        rule = _rule([_acled(country="Ukraine")])
        _patch_campaigns(monkeypatch, {"UKRAINE": [_campaign("Gamaredon Group")]})
        assert rule.run() == []

    def test_mixed_matches_keep_only_attributed(self, monkeypatch):
        rule = _rule([_acled()])
        _patch_campaigns(monkeypatch, {"SYRIA": [
            _campaign("Lazarus Group"),
            _campaign("Syrian Electronic Army"),
        ]})
        correlations = rule.run()
        assert len(correlations) == 1
        assert correlations[0]["cyber_event"]["apt_group"] == "Syrian Electronic Army"


class TestConfidenceEvidence:
    """Confidence scoring and evidence references on the correlation."""

    def test_evidence_references_conflicts_and_campaigns(self, monkeypatch):
        rule = _rule([
            _acled(fatalities=12),
            _acled(fatalities=3, location="Homs"),
        ])
        _patch_campaigns(monkeypatch, {"SYRIA": [_campaign()]})

        corr = rule.run()[0]

        conflict_refs = [e for e in corr["evidence"] if e["kind"] == "conflict"]
        assert len(conflict_refs) == 2
        assert {r["doc_id"] for r in conflict_refs} == {"doc-0", "doc-1"}
        assert all(
            r["index"] == "geon-acled-events-2026.06" for r in conflict_refs
        )
        assert "fatalities" in conflict_refs[0]["summary"]

        cyber_refs = [e for e in corr["evidence"] if e["kind"] == "cyber"]
        assert len(cyber_refs) == 1
        assert cyber_refs[0]["index"] == "opencti"
        assert cyber_refs[0]["doc_id"].startswith("intrusion-set--")
        assert "Syrian Electronic Army" in cyber_refs[0]["summary"]

    def test_evidence_capped_per_source_type(self, monkeypatch):
        events = [dict(_acled(), country="Russia") for _ in range(7)]
        campaigns = [
            _campaign(name=n)
            for n in ("APT28", "APT29", "Sandworm Team", "Turla")
        ]
        rule = _rule(events)
        _patch_campaigns(monkeypatch, {"RUSSIA": campaigns})

        kinds = [e["kind"] for e in rule.run()[0]["evidence"]]
        assert kinds.count("conflict") == 5  # capped at 5
        assert kinds.count("cyber") == 3     # capped at 3

    def test_proximity_factor_uses_real_gap(self, monkeypatch):
        """Latest conflict 2026-06-08, campaign modified 2026-06-09: the
        1-day gap is scored against the rule's CYBER_WINDOW_DAYS."""
        rule = _rule([
            _acled(event_date="2026-06-01T00:00:00+00:00"),
            _acled(event_date="2026-06-08T00:00:00+00:00"),
        ])
        _patch_campaigns(monkeypatch, {
            "SYRIA": [_campaign(modified="2026-06-09T00:00:00+00:00")],
        })

        factors = rule.run()[0]["confidence_factors"]

        expected = 15.0 * (1 - 1 / CYBER_WINDOW_DAYS)
        assert factors["proximity"] == pytest.approx(expected)

    def test_unparseable_dates_drop_proximity_bonus(self, monkeypatch):
        rule = _rule([_acled(event_date="not-a-date")])
        _patch_campaigns(monkeypatch, {"SYRIA": [_campaign(modified="")]})
        factors = rule.run()[0]["confidence_factors"]
        assert factors["proximity"] == 0.0

    def test_attribution_factor_is_opencti_grade(self, monkeypatch):
        """Matches are STIX-validated against the attribution map, so the
        attribution factor uses the strongest ("opencti") bonus."""
        rule = _rule([_acled()])
        _patch_campaigns(monkeypatch, {"SYRIA": [_campaign()]})
        factors = rule.run()[0]["confidence_factors"]
        assert factors["attribution"] == 30.0


class TestSeverity:
    @pytest.mark.parametrize(
        ("num_events", "fatalities", "matches", "expected"),
        [
            # 2 + 2 + 1 + 1 = 6 -> critical
            (50, 100, [_campaign(confidence=85)] * 3, "critical"),
            # 2 + 2 = 4 -> high
            (50, 100, [_campaign(confidence=10)], "high"),
            # 1 + 1 = 2 -> medium
            (10, 10, [_campaign(confidence=10)], "medium"),
            # just below every bump -> low
            (9, 9, [_campaign(confidence=79)], "low"),
            # cyber-only bumps: 1 + 1 = 2 -> medium
            (1, 0, [_campaign(confidence=85)] * 3, "medium"),
        ],
    )
    def test_severity_matrix(self, num_events, fatalities, matches, expected):
        result = ConflictCyberRule._compute_severity(num_events, fatalities, matches)
        assert result == expected


class TestTimeline:
    def test_timeline_sorted_and_capped(self, monkeypatch):
        events = [
            _acled(event_date=f"2026-06-{d:02d}T00:00:00+00:00", fatalities=1)
            for d in (7, 3, 9, 1, 5, 2, 8)  # 7 events, deliberately unsorted
        ]
        # RUSSIA has 4+ attributed APTs in the mapping — enough to
        # exercise the cyber-timeline cap with validated matches.
        events = [dict(e, country="Russia") for e in events]
        campaigns = [
            _campaign(name=name, modified=f"2026-06-{10 + i}T00:00:00+00:00")
            for i, name in enumerate(
                ["APT28", "APT29", "Sandworm Team", "Turla"])
        ]
        rule = _rule(events)
        _patch_campaigns(monkeypatch, {"RUSSIA": campaigns})

        timeline = rule.run()[0]["timeline"]

        types = [t["type"] for t in timeline]
        assert types.count("conflict") == 5  # capped at 5
        assert types.count("cyber") == 3     # capped at 3
        dates = [t["date"] for t in timeline]
        assert dates == sorted(dates)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
