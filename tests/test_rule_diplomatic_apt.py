"""Unit tests for correlation Rule 1: diplomatic escalation + APT activity.

Pure logic, mocked Elasticsearch and monkeypatched OpenCTI helper —
no live network calls. Severity matrix and id stability are covered in
test_correlation_engine.py / test_correlation_ids.py; this file covers
run() end-to-end behaviour: querying, grouping, APT-mapping validation
and correlation assembly.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingestors"))

import pytest
from correlation.rules.diplomatic_apt import (
    APT_WINDOW_DAYS,
    GOLDSTEIN_THRESHOLD,
    DiplomaticAPTRule,
)

SEVERITIES = {"low", "medium", "high", "critical"}


def _event(
    src: str = "RUSSIA",
    tgt: str = "UKRAINE",
    goldstein: float = GOLDSTEIN_THRESHOLD - 3.0,
    **overrides: Any,
) -> dict[str, Any]:
    evt = {
        "event_id": "1234567890",
        "date": "2026-06-10T12:00:00+00:00",
        "source_country": src,
        "target_country": tgt,
        "goldstein_scale": goldstein,
        "cameo_description": "Military force deployment",
    }
    evt.update(overrides)
    return evt


def _hits(sources: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap _source docs in the exact ES search response shape."""
    return {
        "hits": {
            "hits": [
                {"_id": f"doc-{i}", "_source": s} for i, s in enumerate(sources)
            ]
        }
    }


def _campaign(name: str = "APT28", confidence: int = 85) -> dict[str, Any]:
    return {
        "name": name,
        "id": f"intrusion-set--{name.lower().replace(' ', '-')}",
        "confidence": confidence,
        "_geon_type": "intrusion-set",
        "modified": "2026-06-08T00:00:00+00:00",
    }


def _rule(search_response: dict[str, Any] | None = None) -> DiplomaticAPTRule:
    es = MagicMock()
    es.search.return_value = search_response if search_response is not None else _hits([])
    return DiplomaticAPTRule(es=es, octi=None)


def _patch_campaigns(monkeypatch: pytest.MonkeyPatch, per_country: dict[str, list]):
    """Patch the helper imported into the rule's module namespace."""
    calls: list[tuple[str, int]] = []

    def fake(octi: Any, country: str, days_back: int = 30) -> list[dict[str, Any]]:
        calls.append((country, days_back))
        return per_country.get(country, [])

    monkeypatch.setattr(
        "correlation.rules.diplomatic_apt.get_campaigns_by_country", fake
    )
    return calls


class TestHappyPath:
    def test_escalation_with_validated_apt_yields_correlation(self, monkeypatch):
        worst = GOLDSTEIN_THRESHOLD - 3.0
        rule = _rule(_hits([
            _event(goldstein=worst),
            _event(goldstein=GOLDSTEIN_THRESHOLD - 1.0, event_id="222"),
        ]))
        calls = _patch_campaigns(monkeypatch, {"RUSSIA": [_campaign("APT28")]})

        correlations = rule.run()

        assert len(correlations) == 1
        corr = correlations[0]
        assert corr["rule_name"] == "diplomatic_escalation_apt"
        assert corr["severity"] in SEVERITIES
        assert corr["countries_involved"] == ["RUSSIA", "UKRAINE"]
        assert len(corr["correlation_id"]) == 20
        assert "APT28" in corr["description"]
        assert "RUSSIA" in corr["description"] and "UKRAINE" in corr["description"]
        # Worst (lowest Goldstein) event anchors the diplomatic side.
        assert corr["diplomatic_event"]["goldstein"] == worst
        assert corr["diplomatic_event"]["event_id"] == "1234567890"
        assert corr["cyber_event"]["apt_group"] == "APT28"
        # Timeline: one diplomatic + one cyber entry.
        types = [t["type"] for t in corr["timeline"]]
        assert types.count("diplomatic") == 1
        assert types.count("cyber") == 1
        # OpenCTI queried for both countries with the configured window.
        assert ("RUSSIA", APT_WINDOW_DAYS) in calls
        assert ("UKRAINE", APT_WINDOW_DAYS) in calls

    def test_reversed_pair_groups_into_single_correlation(self, monkeypatch):
        rule = _rule(_hits([
            _event(src="RUSSIA", tgt="UKRAINE"),
            _event(src="UKRAINE", tgt="RUSSIA", event_id="333"),
        ]))
        _patch_campaigns(monkeypatch, {"RUSSIA": [_campaign("APT28")]})

        correlations = rule.run()
        assert len(correlations) == 1
        assert correlations[0]["countries_involved"] == ["RUSSIA", "UKRAINE"]


class TestThresholdEdge:
    def test_query_delegates_cutoff_to_es_with_config_threshold(self, monkeypatch):
        """The Goldstein cutoff is applied in the ES query (strict ``lt``),
        so the config constant must appear verbatim in the request."""
        rule = _rule(_hits([]))
        _patch_campaigns(monkeypatch, {})

        rule.run()

        query = rule.es.search.call_args.kwargs["query"]
        must = query["bool"]["must"]
        assert {"range": {"goldstein_scale": {"lt": GOLDSTEIN_THRESHOLD}}} in must
        # Countryless events are excluded.
        must_not = query["bool"]["must_not"]
        assert {"term": {"source_country": ""}} in must_not
        assert {"term": {"target_country": ""}} in must_not

    def test_escalation_without_apt_activity_yields_nothing(self, monkeypatch):
        rule = _rule(_hits([_event()]))
        _patch_campaigns(monkeypatch, {})  # no campaigns for any country
        assert rule.run() == []


class TestEmptyData:
    def test_no_gdelt_hits_returns_empty(self, monkeypatch):
        rule = _rule(_hits([]))
        fake = MagicMock()
        monkeypatch.setattr(
            "correlation.rules.diplomatic_apt.get_campaigns_by_country", fake
        )
        assert rule.run() == []
        fake.assert_not_called()


class TestAptMappingValidation:
    """OpenCTI returns unfiltered matches; every APT must be validated
    against country_apt_mapping.json before it can correlate."""

    def test_foreign_apt_rejected(self, monkeypatch):
        # Lazarus Group is attributed to NORTH KOREA, not RUSSIA/UKRAINE.
        rule = _rule(_hits([_event()]))
        _patch_campaigns(monkeypatch, {"RUSSIA": [_campaign("Lazarus Group")]})
        assert rule.run() == []

    def test_mixed_matches_keep_only_mapped_apts(self, monkeypatch):
        rule = _rule(_hits([_event()]))
        _patch_campaigns(monkeypatch, {
            "RUSSIA": [_campaign("Lazarus Group"), _campaign("APT28", confidence=60)],
        })

        correlations = rule.run()
        assert len(correlations) == 1
        corr = correlations[0]
        assert corr["cyber_event"]["apt_group"] == "APT28"
        cyber_descriptions = [
            t["description"] for t in corr["timeline"] if t["type"] == "cyber"
        ]
        assert all("Lazarus" not in d for d in cyber_descriptions)

    def test_validation_is_case_insensitive(self, monkeypatch):
        rule = _rule(_hits([_event()]))
        _patch_campaigns(monkeypatch, {"RUSSIA": [_campaign("apt28")]})
        assert len(rule.run()) == 1

    def test_pair_with_no_mapping_entries_yields_nothing(self, monkeypatch):
        """Countries absent from the mapping cannot be validated — the rule
        must drop the match instead of risking a false positive."""
        rule = _rule(_hits([_event(src="FRANCE", tgt="GERMANY")]))
        _patch_campaigns(monkeypatch, {
            "FRANCE": [_campaign("APT28")],  # plausible-looking but unverifiable
        })
        assert rule.run() == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
