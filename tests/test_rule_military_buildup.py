"""Tests for correlation Rule 6 (military spending surge + APT activity).

Elasticsearch is mocked; the OpenCTI helper is monkeypatched in the rule's
own module namespace (it is imported INTO ``military_buildup``).
"""

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingestors"))

import pytest
from correlation.rules.military_buildup import (
    _COUNTRY_APT_MAP,
    SPENDING_INDEX,
    YOY_THRESHOLD,
    MilitaryBuildupRule,
)

CURRENT_YEAR = datetime.now(tz=timezone.utc).year


def _hits(docs):
    return {"hits": {"hits": [{"_id": f"id-{i}", "_source": d} for i, d in enumerate(docs)]}}


def _spending(country="RUSSIA", yoy=None, year=None, usd=86000.0):
    return {
        "country": country,
        "spending_change_yoy_pct": yoy if yoy is not None else YOY_THRESHOLD + 5.0,
        "year": year if year is not None else CURRENT_YEAR,
        "spending_usd_millions": usd,
    }


def _assert_scoring_contract(corr):
    """Every correlation carries auditable confidence + evidence refs."""
    assert isinstance(corr["confidence"], int)
    assert 5 <= corr["confidence"] <= 95
    assert isinstance(corr["confidence_factors"], dict)
    assert "base" in corr["confidence_factors"]
    assert corr["evidence"]
    for ev in corr["evidence"]:
        assert {"index", "doc_id", "kind", "summary"} <= set(ev)


def _make_rule(spending_docs, octi=None):
    es = MagicMock()
    es.search.return_value = _hits(spending_docs)
    return MilitaryBuildupRule(es=es, octi=octi)


class TestRunHappyPath:
    def test_spending_surge_with_attributed_apts_fires(self):
        """No OpenCTI: a known APT country falls back to the static map."""
        rule = _make_rule([_spending()])

        out = rule.run()

        assert len(out) == 1
        corr = out[0]
        assert corr["rule_name"] == "military_buildup_cyber"
        assert corr["severity"] in {"medium", "high"}
        assert corr["countries_involved"] == ["RUSSIA"]
        assert corr["correlation_id"]
        assert "RUSSIA increased military spending" in corr["description"]
        # Static fallback: first mapped APT, no campaign id.
        assert corr["cyber_event"]["apt_group"] == _COUNTRY_APT_MAP["RUSSIA"][0]
        assert corr["cyber_event"]["campaign_id"] == ""
        assert corr["timeline"][0]["type"] == "military_spending"
        assert corr["timeline"][0]["date"].startswith(str(CURRENT_YEAR))

    def test_confidence_and_evidence_contract_static_path(self):
        """Static-map attribution: spending doc ref + cyber refs, with the
        weaker static attribution bonus and the YoY-strength factor."""
        rule = _make_rule([_spending()])  # yoy = threshold + 5

        corr = rule.run()[0]

        _assert_scoring_contract(corr)
        spending_ev = corr["evidence"][0]
        assert spending_ev["kind"] == "spending"
        assert spending_ev["index"] == SPENDING_INDEX
        # Deterministic {country}:{year} identity of the SIPRI doc.
        assert spending_ev["doc_id"] == f"RUSSIA:{CURRENT_YEAR}"
        cyber_evs = [e for e in corr["evidence"] if e["kind"] == "cyber"]
        assert cyber_evs
        assert len(cyber_evs) <= 5
        assert all(e["index"] == "static" for e in cyber_evs)
        factors = corr["confidence_factors"]
        assert factors["base"] == 30.0
        assert factors["attribution"] == 15.0  # static map only
        assert factors["yoy_strength"] == 5.0  # yoy 5 points above threshold


class TestQueryShape:
    def test_spending_query_pins_yoy_threshold_and_recent_years(self):
        """The YoY threshold is enforced server-side: pin the strict ``gt``
        on the configured constant and the current/previous-year filter."""
        rule = _make_rule([])
        rule.run()

        kwargs = rule.es.search.call_args.kwargs
        assert kwargs["index"] == SPENDING_INDEX
        filters = kwargs["query"]["bool"]["filter"]
        assert {"range": {"spending_change_yoy_pct": {"gt": YOY_THRESHOLD}}} in filters
        assert {"range": {"year": {"gte": CURRENT_YEAR - 1}}} in filters


class TestNoAptAttribution:
    def test_country_without_apt_mapping_is_skipped(self):
        """High spender with no attributed APT groups must not fire."""
        assert "FRANCE" not in _COUNTRY_APT_MAP  # fixture sanity
        rule = _make_rule([_spending(country="FRANCE")])
        assert rule.run() == []


class TestEmptyData:
    def test_no_high_spenders_returns_empty(self):
        rule = _make_rule([])
        assert rule.run() == []
        assert rule.es.search.call_count == 1

    def test_es_error_returns_empty(self):
        rule = MilitaryBuildupRule(es=MagicMock(), octi=None)
        rule.es.search.side_effect = RuntimeError("es down")
        assert rule.run() == []


class TestOpenCTIValidation:
    """OpenCTI campaigns are validated against the static country->APT map
    to avoid cross-contamination; non-matching names fall back to static."""

    def test_validated_campaign_used(self, monkeypatch):
        campaigns = [
            {"name": "Lazarus Group", "id": "intrusion-set--nk"},  # North Korea, not Russia
            {"name": "APT28", "id": "intrusion-set--abc"},
        ]
        monkeypatch.setattr(
            "correlation.rules.military_buildup.get_campaigns_by_country",
            lambda octi, country, days_back=365: campaigns,
        )
        rule = _make_rule([_spending()], octi=MagicMock())

        out = rule.run()

        assert len(out) == 1
        corr = out[0]
        # Lazarus Group is filtered out; the validated APT28 wins.
        assert corr["cyber_event"]["apt_group"] == "APT28"
        assert corr["cyber_event"]["campaign_id"] == "intrusion-set--abc"
        # STIX-validated attribution scores higher than the static map.
        assert corr["confidence_factors"]["attribution"] == 30.0
        cyber_evs = [e for e in corr["evidence"] if e["kind"] == "cyber"]
        assert [e["index"] for e in cyber_evs] == ["opencti"]
        assert cyber_evs[0]["doc_id"] == "intrusion-set--abc"

    def test_unvalidated_campaigns_fall_back_to_static(self, monkeypatch):
        monkeypatch.setattr(
            "correlation.rules.military_buildup.get_campaigns_by_country",
            lambda octi, country, days_back=365: [{"name": "Lazarus Group", "id": "x"}],
        )
        rule = _make_rule([_spending()], octi=MagicMock())

        out = rule.run()

        assert len(out) == 1
        assert out[0]["cyber_event"]["apt_group"] == _COUNTRY_APT_MAP["RUSSIA"][0]
        assert out[0]["cyber_event"]["campaign_id"] == ""

    def test_octi_error_falls_back_to_static(self, monkeypatch):
        def boom(*_args, **_kwargs):
            raise RuntimeError("opencti down")

        monkeypatch.setattr(
            "correlation.rules.military_buildup.get_campaigns_by_country", boom
        )
        rule = _make_rule([_spending()], octi=MagicMock())

        out = rule.run()

        assert len(out) == 1
        assert out[0]["cyber_event"]["apt_group"] == _COUNTRY_APT_MAP["RUSSIA"][0]


class TestSeverityTiers:
    def test_above_20_pct_is_high(self):
        rule = object.__new__(MilitaryBuildupRule)
        corr = rule._build_correlation(_spending(yoy=25.0), [{"name": "APT28"}])
        assert corr["severity"] == "high"

    def test_moderate_increase_is_medium(self):
        rule = object.__new__(MilitaryBuildupRule)
        corr = rule._build_correlation(_spending(yoy=15.0), [{"name": "APT28"}])
        assert corr["severity"] == "medium"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
